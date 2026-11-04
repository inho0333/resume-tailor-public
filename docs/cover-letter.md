# Cover letters

`cover_letter.py` renders a Markdown draft into a one-page .docx with a fixed
layout. The writing is yours (or a model's, from the same material as the
résumé); this module only typesets it and checks the page count.

## The structure it expects

A Markdown file of blocks separated by blank lines, in this exact order:

```
NAME
email · phone · city                  ← header block: line 1 name, line 2 contact

Dear Hiring Manager,                  ← salutation (the .docx replaces it with a Re: line)

<paragraph 1>                         ← body

<paragraph 2>

...

Name                                  ← signature: the canonical spelling of your name
```

Fewer than four blocks is an error, not a guess: the parser would otherwise
silently treat your first body paragraph as the contact line.

The signature block, not the header, is taken as the spelling of your name —
headers are often all caps.

## Rendering

```bash
.venv/bin/python cover_letter.py \
  --md output/ExampleCo/COVER_LETTER_ExampleCo_PMIntern.md \
  --company ExampleCo \
  --role "Product Management Intern" \
  --re "Product Management Intern (Summer 2027), Austin, TX"
```

Layout is fixed: Calibri, 0.75" margins, 16pt name / 10pt contact / 11pt body,
single spacing. After rendering, the page count is measured in Word — a cover
letter is held to one page the same way a résumé is.

The output prints the paragraph and word count, which is the number worth
watching. Past about 350 words the page is full and the reader has stopped.

## A formula that works

Five paragraphs, and short sentences — if a sentence runs past twenty words,
split it.

1. **Why this role, in one claim.** Name the team and the posting. No windup.
2. **One project, told properly.** Problem, what you did, the hard part, the
   measured result. One project per company, chosen because it maps onto what
   the posting asks for — not because it is your favourite.
3. **The second, shorter.** A different competence, not a second helping of the
   first.
4. **Why them specifically.** The strongest version of this paragraph is
   evidence from a primary source: their product, their changelog, their
   earnings call, a problem they have publicly said they have. Then connect it
   to the experience of yours that bears on it, and name the team from the
   posting. A paragraph that would read identically addressed to a competitor is
   a wasted paragraph.
5. **Close.** What you want to happen next, in two lines.

The common failure is paragraph 4 written from the company's marketing page.
`company_research.py` output helps here — it extracts what the company says
about itself in its own words — but the reader can tell the difference between a
quote and a conclusion.

## Keeping it honest

The same rule as the résumé applies, and no code enforces it here: **every
figure in a cover letter must be one you can defend.** The validators in
`minimal_tailor.py` guard the résumé because its numbers come from
`resume_db.yaml`. A cover letter drafted by a model from the same material has
no such check, so read it against the database before sending.
