"""Builds `templates/resume_template.docx` — a blank résumé in the layout this tool renders
into.

## Why a template ships at all

Everything is rendered back into *your* document: its fonts, margins, bullet style and section
headings are the output's. So the quality of the output has a floor set by the file you point
at, and two things about that file are not negotiable (`resume_parser.py`):

  - a section heading paragraph reading exactly `EXPERIENCE`
  - inside it, one non-bullet header line per employer (`Company, City`), then its bullets

A résumé written as flowing paragraphs — no heading, no bulleted runs — parses as zero
experience blocks, and `bootstrap` says so and stops. Rather than describe the shape in prose
and leave people to reverse-engineer it, this script writes a document that has it.

## The layout

A standard one-page business-school résumé, measured off a real one:

    margins 0.5" top/bottom, 0.75" left/right     Calibri throughout, 10pt body, 1.15 spacing
    NAME                      centred, bold, 18pt
    contact line              centred, 10.5pt
    SECTION HEADING           bold, 12pt, 6pt above / 3pt below
    Employer, City  →  dates  bold, right tab stop at 7.0"
    one line on the employer  italic, 9.5pt
    Title           →  dates  italic, right tab stop at 7.0"
      • bullets               "List Bullet" style

The right tab stop at 7.0" is what puts dates flush to the right margin (7.0" = 8.5" page less
the two 0.75" margins). Word's own right-align would break the line into two paragraphs, and
the parser would then read the date line as another employer.

## Regenerating

    python templates/make_template.py

Edit this script rather than the .docx when the layout has to change: the .docx is committed so
nobody needs python-docx to get started, but this file is the source of truth for what is in it.
The content is a fictional person consistent with profile.example.yaml and
base_resume.example.yaml — replace it with your own, or keep it and work through
`run.py bootstrap --resume templates/resume_template.docx` first to watch the pipeline run
end to end before your own career data is involved.
"""

import os

import docx
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_TAB_ALIGNMENT
from docx.shared import Inches, Pt

OUT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "resume_template.docx")

BODY_PT = 10.0        # Calibri 10pt, the size the one-page page-count estimate is calibrated to
NAME_PT = 18.0
CONTACT_PT = 10.5
HEADING_PT = 12.0
BLURB_PT = 9.5        # the one-line description under an employer
DATE_TAB = Inches(7.0)


def _p(doc, text="", *, size=None, bold=False, italic=False, align=None,
       space_before=None, space_after=None, style=None, tab_text=None):
    """One paragraph. `tab_text` is placed at the right tab stop (the dates column)."""
    para = doc.add_paragraph(style=style)
    if align is not None:
        para.alignment = align
    fmt = para.paragraph_format
    if space_before is not None:
        fmt.space_before = Pt(space_before)
    if space_after is not None:
        fmt.space_after = Pt(space_after)
    if tab_text:
        fmt.tab_stops.add_tab_stop(DATE_TAB, WD_TAB_ALIGNMENT.RIGHT)
        text = f"{text}\t{tab_text}"
    run = para.add_run(text)
    run.bold = bold
    run.italic = italic
    run.font.name = "Calibri"
    run.font.size = Pt(size or BODY_PT)
    return para


def _section(doc, title):
    return _p(doc, title, size=HEADING_PT, bold=True, space_before=6, space_after=3)


def _employer(doc, header_line, blurb, title, dates):
    """The three lines the parser reads as one employer block.

    `header_line` is `header_text` in resume_db.yaml and base_resume.yaml — it has to match
    this paragraph character for character, which is why `bootstrap` copies it out of the
    document rather than asking anyone to retype it.
    """
    _p(doc, header_line, bold=True, space_before=4)
    if blurb:
        _p(doc, blurb, size=BLURB_PT, italic=True, space_after=2)
    _p(doc, title, italic=True, tab_text=dates, space_after=2)


def build(path=OUT_PATH):
    doc = docx.Document()

    section = doc.sections[0]
    section.top_margin = section.bottom_margin = Inches(0.5)
    section.left_margin = section.right_margin = Inches(0.75)

    normal = doc.styles["Normal"]
    normal.font.name = "Calibri"
    normal.font.size = Pt(BODY_PT)
    normal.paragraph_format.line_spacing = 1.15
    normal.paragraph_format.space_after = Pt(0)

    bullet = doc.styles["List Bullet"]
    bullet.font.name = "Calibri"
    bullet.font.size = Pt(BODY_PT)
    bullet.paragraph_format.space_after = Pt(0)
    bullet.paragraph_format.line_spacing = 1.15

    _p(doc, "JANE DOE", size=NAME_PT, bold=True,
       align=WD_ALIGN_PARAGRAPH.CENTER, space_after=2)
    _p(doc, "jane.doe@example.com  |  +1-555-0100  |  linkedin.com/in/example",
       size=CONTACT_PT, align=WD_ALIGN_PARAGRAPH.CENTER, space_after=6)

    _section(doc, "EDUCATION")
    _p(doc, "Example University, Example School of Business", bold=True, tab_text="May 2027")
    _p(doc, "Master of Business Administration", italic=True, space_after=2)
    _p(doc, "Northgate University", bold=True, tab_text="Feb. 2019")
    _p(doc, "Bachelor of Science in Economics", italic=True, space_after=2)

    # The heading the parser looks for. Pass a different one with
    # `bootstrap --section-heading "WORK EXPERIENCE"` if you rename it.
    _section(doc, "EXPERIENCE")

    _employer(doc,
              "ACME CORP., Austin, TX",
              "Payments platform settling $2B a year for 12,000 merchants across marketplace, "
              "fintech and support",
              "Senior Associate, Payments Product", "Jul. 2022 - Aug. 2026")
    # These sentences are the ones in base_resume.example.yaml and resume_db.example.yaml,
    # character for character: one fictional person across every example file. They also sit
    # inside the house style the validators enforce (210-250 characters), which a template has
    # to do — an edit may not get longer than the sentence it replaces, so a 278-character
    # bullet leaves no room for one.
    doc.add_paragraph(
        "Migrated billing from a monthly to a weekly cycle for 12,000 merchants, after churn "
        "interviews showed payout speed drove attrition, by modelling the working-capital "
        "impact and rebuilding settlement to carry corrections, cutting merchant churn 18%.",
        style="List Bullet")
    doc.add_paragraph(
        "Rebuilt support routing around fault attribution rather than inquiry wording, after a "
        "transaction surge made headcount the default answer, by interviewing agents to build "
        "a taxonomy and extending settlement to partial orders, automating 18% of volume.",
        style="List Bullet")

    _employer(doc,
              "EXAMPLE VENTURES, Singapore",
              "Early-stage fund with $200M under management, investing across Southeast Asia",
              "Investment Associate", "Jul. 2019 - Jul. 2022")
    doc.add_paragraph(
        "Invested $12.4M across 28 startups after building a bottom-up demand model, when "
        "three failed deals showed founder projections went unchallenged, pricing each round "
        "on evidence of willingness to pay rather than claimed market size, at a 33% "
        "follow-on rate.", style="List Bullet")
    doc.add_paragraph(
        "Built the firm's first diligence scorecard to keep pace with a doubling deal "
        "pipeline, after two partners were screening to different standards, by codifying the "
        "questions that had predicted failure, cutting time to a first decision from 3 weeks "
        "to 8 days.", style="List Bullet")

    _section(doc, "ADDITIONAL")
    doc.add_paragraph(
        "Advised 30+ early-stage founders on product strategy as a university startup-centre "
        "mentor.", style="List Bullet")
    # Kept verbatim by the renderer: the Skills and Languages lines are never reworded or
    # reordered (minimal_tailor.PROTECTED_PREFIXES).
    doc.add_paragraph(
        "Skills: Data Analysis (Python, SQL), Visualisation (Tableau)  |  "
        "Languages: English, Spanish", style="List Bullet")

    doc.save(path)
    return path


if __name__ == "__main__":
    print(f"wrote {build()}")
