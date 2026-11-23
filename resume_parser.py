"""Pulls the company blocks (header + bullets) of the EXPERIENCE section out of an uploaded
resume .docx automatically.

This is the opposite direction from resume_writer.find_section_block — instead of locating a
block by a header_text it already knows, it guesses the header/bullet boundaries from
scratch (`setup` / `bootstrap` only). It is a heuristic that assumes a standard one-page
resume layout (company + location line -> [description line] -> [title + dates line] ->
bullets, with flat sections such as "ADDITIONAL" among them), so it may not be perfect —
setup shows the result to a human for confirmation.

## Where a block ends

A block ends at the next bulleted run, or at the next **section heading**. The second half of
that sentence is not cosmetic. The scan runs from the experience heading to the end of the
document, and without it a block's header lines run straight through any later heading:

    WORK EXPERIENCE          <- the scan starts here
      NORTHWIND DATA, ...
        • bullet
      HELIOS ROBOTICS, ...
        • bullet
    EDUCATION                <- became an "employer"...
      Carnegie Institute ...
      MS Computer Science
    ADDITIONAL               <- ...which swallowed this heading as one of its own lines,
        • Open Source: ...      and then claimed these bullets as EDUCATION's

Measured on four ordinary résumés, all four hit it, because every one of them puts education
*after* experience — which is how most experienced-hire résumés are laid out, and the
opposite of the shipped template. The result was a database whose ADDITIONAL bullets were
filed under a section called EDUCATION, and a renderer that would then write them there.

A heading is recognised by the document's own formatting rather than by a list of words: the
paragraph that matched `start_after` IS a section heading, so its bold/size/style signature is
what the others are compared against. Nothing here needs to know that "EDUCATION" is a word.
"""

from docx.oxml.ns import qn


def _numpr(paragraph):
    return paragraph._p.find(f".//{qn('w:numPr')}")


def is_bullet(paragraph):
    """Whether this is a bulleted paragraph — **either numPr sits directly on the paragraph,
    or the paragraph uses a list style ("List Bullet" and friends)**.

    Both cases occur in real documents: Word writes numPr onto the paragraph when you click
    the bullet button, but a résumé built from a template often gets its glyph from the style
    instead. Checking only numPr made bootstrap report "found no experience blocks" on a
    perfectly ordinary résumé. This matches resume_writer.is_bullet deliberately: the parser
    and the renderer must agree on what a bullet is, or bootstrap builds a database the
    renderer cannot write back into.
    """
    if _numpr(paragraph) is not None:
        return True
    try:
        return (paragraph.style.name or "").startswith("List")
    except (AttributeError, KeyError):
        return False


def format_signature(paragraph):
    """(bold, size in points, style name) of a paragraph's first non-empty run.

    How a section heading is told apart from a body line, using the document's own
    formatting instead of a vocabulary. None when the paragraph has no text runs.
    """
    runs = [r for r in paragraph.runs if r.text.strip()]
    if not runs:
        return None
    run = runs[0]
    size = run.font.size
    try:
        style = paragraph.style.name or ""
    except (AttributeError, KeyError):
        style = ""
    return (bool(run.bold), size.pt if size is not None else None, style)


def find_heading_index(paragraphs, heading_text):
    """Index of the first paragraph matching heading_text (case-insensitive). None if absent."""
    needle = heading_text.strip().lower()
    for i, p in enumerate(paragraphs):
        if p.text.strip().lower() == needle:
            return i
    return None


def parse_experience_blocks(doc, start_after="EXPERIENCE"):
    """Scans from the paragraph after start_after to the end of the document, looking for
    "header line(s) + bullet lines" blocks.

    Returns: [{"header_text": str, "context_lines": [str, ...], "bullets": [str, ...]}, ...]
    header_text = the first non-bullet line of each block (exactly the string
    resume_writer.find_section_block matches on) — the company/location line, or, for a flat
    section such as "ADDITIONAL", the section title itself.

    A block with no bullets is dropped, which is what keeps an EDUCATION section out of the
    experience database: it has header lines and no bullets of its own.
    """
    paragraphs = doc.paragraphs
    lo = 0
    heading_sig = None
    if start_after:
        idx = find_heading_index(paragraphs, start_after)
        if idx is None:
            raise ValueError(
                f"Could not find the '{start_after}' section title in the resume. Check that "
                f"the document has a paragraph reading exactly '{start_after}' (or pass yours "
                f"with --section-heading), that each employer is a 'Company, City' line, and "
                f"that the bullets under it are bulleted paragraphs. "
                f"templates/resume_template.docx is a document in that shape."
            )
        lo = idx + 1
        heading_sig = format_signature(paragraphs[idx])
    hi = len(paragraphs)

    blocks = []
    i = lo
    while i < hi:
        p = paragraphs[i]
        text = p.text.strip()
        if not text or is_bullet(p):
            i += 1
            continue

        context_lines = [text]
        j = i + 1
        while j < hi and paragraphs[j].text.strip() and not is_bullet(paragraphs[j]):
            # A later section heading ends this block rather than joining it. See
            # the module docstring: without this, a heading and everything under it
            # are absorbed into whatever block came before.
            if heading_sig and format_signature(paragraphs[j]) == heading_sig:
                break
            context_lines.append(paragraphs[j].text.strip())
            j += 1

        bullets = []
        while j < hi and is_bullet(paragraphs[j]):
            bt = paragraphs[j].text.strip()
            if bt:
                bullets.append(bt)
            j += 1

        if bullets:
            blocks.append({
                "header_text": context_lines[0],
                "context_lines": context_lines,
                "bullets": bullets,
            })
        i = j if j > i else i + 1

    return blocks


def section_name_from_header(header_text):
    """Extracts a short section name from header_text (same convention as resume_db.yaml):
    "ACME CORP. (Acme Group), Austin, TX" -> "ACME CORP."
    "EXAMPLE CO., Springfield, IL" -> "EXAMPLE CO."
    "ADDITIONAL" -> "ADDITIONAL" (unchanged when there is no comma or parenthesis)
    """
    name = header_text.split(",")[0].split("(")[0].strip()
    return name or header_text.strip()
