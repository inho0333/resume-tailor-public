import copy

from docx.oxml.ns import qn
from docx.text.paragraph import Paragraph


def _base_run_index(runs):
    for i, run in enumerate(runs):
        if run.text.strip():
            return i
    return 0


def _strip_east_asia_hint(run):
    """Removes w:hint="eastAsia" from the base run's rFonts if it is still there.

    In the source document that hint was formatting applied to a specific, short span of
    characters. But when several runs are merged into one and the (much longer) replacement
    text all lands under that run, ambiguous punctuation in the new text — an em dash (—) or a
    curly quote — gets rendered in the East Asian font rather than the body font because of
    the hint, and that one line ends up with different line spacing from the rest.
    If the body text contains no actual East Asian characters, deleting the hint makes no
    visual difference, so it is safe to remove."""
    rPr = run._element.rPr
    if rPr is None:
        return
    rFonts = rPr.find(qn("w:rFonts"))
    if rFonts is not None and rFonts.get(qn("w:hint")) == "eastAsia":
        rPr.remove(rFonts)


def _numpr(paragraph):
    return paragraph._p.find(f".//{qn('w:numPr')}")


def is_bullet(paragraph):
    """Whether this is a bulleted paragraph — **either numPr sits directly on the paragraph,
    or the paragraph uses a list style (List ...)**.

    Measured once: in a newer approved resume (`your_resume.docx`) the EXPERIENCE bullets did
    not carry numPr on the paragraph at all; they got their bullet glyph from the `List
    Bullet` style. The old test, which only looked at numPr, did not see those paragraphs as
    bullets, so `find_section_block` failed to find the ACME CORP./EXAMPLE CO. blocks and
    **picked up the bullet block of the next section (ADDITIONAL)** instead, rendering there.
    The output was character-for-character identical to the approved document and still
    reported "PASS" (the edits and swaps had vanished entirely and nobody could tell)."""
    if _numpr(paragraph) is not None:
        return True
    try:
        return (paragraph.style.name or "").startswith("List")
    except (AttributeError, KeyError):
        return False


def find_section_block(doc, header_text, stop_texts=()):
    """Finds the paragraph that exactly matches header_text, then the bullet paragraphs (the
    ones with numPr) that follow it. The context lines between the header and the first bullet
    (e.g. title/dates) are left untouched, and the last context line of that block is used as
    the insertion anchor (section/header boundary detection — moved over from resume_parser).

    stop_texts: the set of header_text values of the other sections. It stops the branch below
    — the one that scoops up paragraphs without numPr as "bullets with broken formatting" —
    from **swallowing the next section's header** (a section whose header is followed by a
    bullet with no context line in between, e.g. ADDITIONAL, was getting sucked into the
    previous section's bullet block and deleted).

    Returns: (anchor_paragraph, [bullet_paragraph, ...])
    """
    paragraphs = doc.paragraphs
    header_idx = None
    for i, p in enumerate(paragraphs):
        if p.text.strip() == header_text:
            header_idx = i
            break
    if header_idx is None:
        raise ValueError(f"Could not find the section header in the template: {header_text!r}")

    j = header_idx + 1
    while j < len(paragraphs) and not is_bullet(paragraphs[j]):
        j += 1
    if j >= len(paragraphs):
        raise ValueError(f"No bullet paragraph follows section {header_text!r}")
    # If **another section's header** sits between the header and the first bullet, then this
    # section has no bullet block and we picked up the next section's block — rendering into
    # it would overwrite that other section (a real incident).
    for k in range(header_idx + 1, j):
        if paragraphs[k].text.strip() in stop_texts:
            raise ValueError(
                f"Could not find the bullet block for section {header_text!r} — the next "
                f"section header ({paragraphs[k].text.strip()!r}) comes first. Check the "
                f"bullet formatting in the template.")
    anchor = paragraphs[j - 1]

    bullets = []
    while j < len(paragraphs):
        if is_bullet(paragraphs[j]):
            bullets.append(paragraphs[j])
            j += 1
        elif paragraphs[j].text.strip() == "":
            # Skip empty paragraphs (do not treat them as the end of the bullet block)
            j += 1
        elif paragraphs[j].text.strip() in stop_texts:
            # The next section's header — this section's bullet block ends here
            break
        elif j + 1 < len(paragraphs) and is_bullet(paragraphs[j + 1]):
            # A paragraph without numPr, but a bullet follows it -> treat as a broken bullet
            print(f"  [warning] including a paragraph with no numPr in the bullet block: "
                  f"{paragraphs[j].text[:60]!r}...")
            bullets.append(paragraphs[j])
            j += 1
        else:
            break

    return anchor, bullets


def _clone_bullet_paragraph(template_p, parent, text):
    """Clones template_p (a deepcopied bullet paragraph element) and returns a new paragraph
    element filled with text. numId/ilvl/font formatting is preserved automatically because
    pPr/rPr are copied as-is."""
    new_p_el = copy.deepcopy(template_p)
    paragraph = Paragraph(new_p_el, parent)
    runs = paragraph.runs
    if not runs:
        raise ValueError("The cloned bullet paragraph has no runs — check the template "
                         "paragraph's formatting")
    base_idx = _base_run_index(runs)
    runs[base_idx].text = text
    _strip_east_asia_hint(runs[base_idx])
    for i, run in enumerate(runs):
        if i != base_idx:
            run.text = ""
    return new_p_el


def render_sections(doc, sections_result, db):
    """sections_result: what tailor_engine.generate_section_revisions() returned,
    {"sections": {name: [{"id":..., "text":...}, ...]}, ...}.
    db: the section list returned by resume_db.load_db() — used to look up header_text and as
    the fallback for empty sections.

    For each section, every existing bullet paragraph is deleted and as many new ones as the
    LLM selected are cloned in (variable-count rendering). One section is handled completely
    — find, delete, insert — before moving to the next: deleting and inserting paragraphs
    shifts all later paragraph indices, so paragraph references captured for an earlier
    section are never reused for the next one (find_section_block is called afresh per
    section).

    Returns: {section_name: [applied bullet id, ...]} — for logging/validation.
    """
    rendered = {}
    other_headers = {s["header_text"] for s in db}
    for section in db:
        name = section["name"]
        bullets_out = sections_result.get("sections", {}).get(name, [])
        if not bullets_out:
            fallback = section["bullets"][0]
            print(f"  [warning] no bullet selected for section '{name}', falling back to the "
                  f"first bullet in the DB: {fallback['id']}")
            bullets_out = [{"id": fallback["id"], "text": fallback["text"]}]

        anchor, existing_bullets = find_section_block(doc, section["header_text"], stop_texts=other_headers)
        template_p = copy.deepcopy(existing_bullets[0]._p)
        parent = anchor._parent

        for p in existing_bullets:
            p._p.getparent().remove(p._p)

        running_anchor = anchor._p
        for item in bullets_out:
            new_p_el = _clone_bullet_paragraph(template_p, parent, item["text"])
            running_anchor.addnext(new_p_el)
            running_anchor = new_p_el

        rendered[name] = [item["id"] for item in bullets_out]

    return rendered


def save(doc, output_path):
    doc.save(output_path)
