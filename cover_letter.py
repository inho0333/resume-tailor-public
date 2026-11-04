"""Renders a cover-letter .md into a .docx with a fixed layout.

The input .md is a sequence of blocks separated by blank lines, in a fixed order:
  1) header (name / contact / program + target role)   2) salutation
  3..) body paragraphs                                 last) signature name
The layout uses the values settled on in a real application (Calibri, 0.75" margins,
16/10/11pt) as-is. After rendering, the page count is measured with Word — a cover letter is
also held to one page.

Usage:
  .venv/bin/python cover_letter.py --md output/ExampleCo/COVER_LETTER_ExampleCo_PMIntern.md \
      --company ExampleCo --role "Product Management Intern" \
      --re "Product Management Intern (Summer 2027), Austin, TX"
"""
import argparse, datetime, os, re

import docx
from docx.shared import Inches, Pt

import pagefit


def _parse(md_path):
    """Returns: (name, contact, body_paragraphs). The header block is read line by line."""
    raw = open(md_path, encoding="utf-8").read()
    blocks = [b for b in raw.split("\n\n") if b.strip()]
    if len(blocks) < 4:
        raise SystemExit(f"Only {len(blocks)} blocks — check the "
                         f"header/salutation/body/signature structure")
    head = [l.strip() for l in blocks[0].strip().splitlines() if l.strip()]
    flat = lambda b: re.sub(r"\s+", " ", b).strip()
    name = flat(blocks[-1])                      # the signature line is the canonical spelling
                                                 # (the header may be all caps)
    body = [flat(b) for b in blocks[2:-1]]       # blocks[1] = salutation (the docx uses a
                                                 # Re: line instead)
    return name, head[1], body


def render(md_path, out_path, recipient, re_line, date_str):
    name, contact, body = _parse(md_path)

    doc = docx.Document()
    section = doc.sections[0]
    for attr in ("top_margin", "bottom_margin", "left_margin", "right_margin"):
        setattr(section, attr, Inches(0.75))
    doc.styles["Normal"].font.name = "Calibri"
    doc.styles["Normal"].font.size = Pt(11)

    def para(text, size=11, after=12):
        p = doc.add_paragraph()
        p.paragraph_format.space_after = Pt(after)
        p.paragraph_format.space_before = Pt(0)
        p.paragraph_format.line_spacing = 1.0
        run = p.add_run(text)
        run.font.name, run.font.size = "Calibri", Pt(size)

    para(name, size=16, after=0)
    para(contact, size=10, after=16)
    para(date_str, after=12)
    para(recipient, after=0)
    para(f"Re: {re_line}", after=16)
    for block in body:
        para(block, after=12)
    para("Sincerely,", after=0)
    para(name, after=0)

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    doc.save(out_path)
    words = sum(len(b.split()) for b in body)
    print(f"Saved: {out_path} ({len(body)} body paragraphs / {words} words)")
    try:
        print(f"Measured in Word: {pagefit.word_pages(out_path)} page(s)")
    except Exception as exc:                      # Word missing, waiting on permission, etc.
        print(f"Page-count measurement failed: {exc} — open it yourself and check it is 1 page")
    return out_path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--md", required=True, help="cover letter body .md")
    ap.add_argument("--company", required=True)
    ap.add_argument("--role", required=True)
    ap.add_argument("--re", dest="re_line", help="Re: line (default: '<role>')")
    ap.add_argument("--recipient", help="addressee (default: '<company> Product Management Team')")
    ap.add_argument("--out", help="output .docx (default: output/<company>/"
                                  "CoverLetter_<company>_<role>_<date>.docx)")
    args = ap.parse_args()

    today = datetime.date.today()
    safe = lambda s: re.sub(r"[^A-Za-z0-9]", "", s)
    out = args.out or os.path.join("output", args.company,
                                   f"CoverLetter_{safe(args.company)}_{safe(args.role)}_"
                                   f"{today.strftime('%Y%m%d')}.docx")
    render(args.md, out,
           args.recipient or f"{args.company} Product Management Team",
           args.re_line or args.role,
           today.strftime("%B %-d, %Y"))


if __name__ == "__main__":
    main()
