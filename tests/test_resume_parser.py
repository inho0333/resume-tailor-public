"""Reading an experience section out of a document nobody wrote for this tool.

The shipped template is one layout. Every test here is a layout that is not it,
because the measured failure was that four out of four ordinary résumés broke on
the same thing: they put education *after* experience, which is how most
experienced-hire résumés are laid out and the opposite of the template.
"""

import os
import sys

import docx
import pytest
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Pt

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import resume_parser as rp

TEMPLATE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "templates", "resume_template.docx")


def _build(paragraphs):
    """paragraphs: [(text, kind)] where kind is "heading" | "line" | "bullet"."""
    doc = docx.Document()
    doc.styles["Normal"].font.size = Pt(10)
    for text, kind in paragraphs:
        if kind == "bullet":
            para = doc.add_paragraph(style="List Bullet")
            run = para.add_run(text)
            run.font.size = Pt(10)
            continue
        para = doc.add_paragraph()
        run = para.add_run(text)
        run.bold = kind == "heading"
        run.font.size = Pt(12 if kind == "heading" else 10)
    return doc


def _names(blocks):
    return [rp.section_name_from_header(b["header_text"]) for b in blocks]


def test_education_after_experience_is_not_an_employer():
    """The bug this file exists for, in its smallest form.

    EDUCATION became an employer, and because a block's header lines ran straight
    through the next heading it also absorbed ADDITIONAL and claimed that
    section's bullets as its own. The database then filed those bullets under
    EDUCATION, and the renderer would have written them there.
    """
    doc = _build([
        ("EXPERIENCE", "heading"),
        ("NORTHWIND DATA, San Francisco, CA", "line"),
        ("Staff Engineer", "line"),
        ("Shipped a thing", "bullet"),
        ("EDUCATION", "heading"),
        ("Carnegie Institute of Technology", "line"),
        ("MS Computer Science", "line"),
        ("ADDITIONAL", "heading"),
        ("Open Source: maintainer of a library", "bullet"),
        ("Technical Skills: Python, PyTorch", "bullet"),
    ])
    blocks = rp.parse_experience_blocks(doc)
    assert _names(blocks) == ["NORTHWIND DATA", "ADDITIONAL"]
    # and ADDITIONAL kept its own bullets
    assert len(blocks[-1]["bullets"]) == 2


def test_a_section_with_no_bullets_is_dropped():
    doc = _build([
        ("EXPERIENCE", "heading"),
        ("ACME, Austin, TX", "line"),
        ("Did a thing", "bullet"),
        ("CERTIFICATIONS", "heading"),
        ("Some certificate, 2024", "line"),
    ])
    assert _names(rp.parse_experience_blocks(doc)) == ["ACME"]


def test_a_renamed_experience_heading_is_accepted():
    doc = _build([
        ("WORK EXPERIENCE", "heading"),
        ("ACME, Austin, TX", "line"),
        ("Did a thing", "bullet"),
    ])
    assert _names(rp.parse_experience_blocks(doc, start_after="WORK EXPERIENCE")) == ["ACME"]
    # and the default heading reports the problem rather than returning nothing,
    # because "no experience blocks" reads as "this résumé is unparseable" when
    # the real answer is "pass --section-heading".
    with pytest.raises(ValueError, match="EXPERIENCE"):
        rp.parse_experience_blocks(doc)


def test_one_employer_is_enough():
    """A contractor or anyone early in a career may have exactly one block."""
    doc = _build([
        ("EXPERIENCE", "heading"),
        ("INDEPENDENT, Singapore", "line"),
        ("Product Designer (contract)", "line"),
        ("Redesigned onboarding", "bullet"),
        ("Ran a design system consolidation", "bullet"),
    ])
    blocks = rp.parse_experience_blocks(doc)
    assert _names(blocks) == ["INDEPENDENT"]
    assert len(blocks[0]["bullets"]) == 2


def test_the_shipped_template_still_parses():
    """The template puts education BEFORE experience, so it never hit the bug —
    which is exactly why the bug survived. It has to keep working."""
    blocks = rp.parse_experience_blocks(docx.Document(TEMPLATE))
    assert _names(blocks) == ["ACME CORP.", "EXAMPLE VENTURES", "ADDITIONAL"]


def test_a_document_with_no_bullets_at_all_finds_nothing():
    """A résumé written as prose. `bootstrap` reports this and stops rather than
    building an empty database."""
    doc = _build([
        ("EXPERIENCE", "heading"),
        ("ACME, Austin, TX", "line"),
        ("I did a number of things at this company over four years.", "line"),
    ])
    assert rp.parse_experience_blocks(doc) == []


def test_the_heading_signature_distinguishes_a_heading_from_a_header_line():
    doc = _build([
        ("EXPERIENCE", "heading"),
        ("ACME, Austin, TX", "line"),
        ("Did a thing", "bullet"),
    ])
    paras = doc.paragraphs
    assert rp.format_signature(paras[0]) != rp.format_signature(paras[1])
    # An empty paragraph has no runs to read a signature from, and must not be
    # mistaken for a heading.
    assert rp.format_signature(doc.add_paragraph()) is None
