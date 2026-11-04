"""Pulls the company blocks (header + bullets) of the EXPERIENCE section out of an uploaded
resume .docx automatically.

This is the opposite direction from resume_writer.find_section_block — instead of locating a
block by a header_text it already knows, it guesses the header/bullet boundaries from
scratch (bootstrap command only). It is a heuristic that assumes a standard one-page resume
layout (company + location line -> [description line] -> [title + dates line] -> bullets,
ending in a flat section such as "ADDITIONAL"), so it may not be perfect — bootstrap shows
the result to a human for confirmation.
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
    """
    paragraphs = doc.paragraphs
    lo = 0
    if start_after:
        idx = find_heading_index(paragraphs, start_after)
        if idx is None:
            raise ValueError(
                f"Could not find the '{start_after}' section title in the resume — "
                f"check that the template has an EXPERIENCE section title."
            )
        lo = idx + 1
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
