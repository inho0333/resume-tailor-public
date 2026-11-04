# The quality gates

Every rule here exists because a model produced something worse without it. They
are enforced in **code**, not in prompt instructions, because prompt
instructions were tried first and did not hold: the same rule stated in the
prompt was violated within a day.

Each gate's failure mode is the same: **discard the edit, keep the sentence the
human already approved.** No gate falls back to the model's version.

Most of this lives in `minimal_tailor.py`. The constants named below are at the
top of that file; change them there, not in a prompt.

---

## Why the design looks like this

A pipeline that rewrote every bullet for every posting produced this, and sent
it:

> Built a bottom-up model at a $500M AUM firm sizing customers x
> willingness-to-pay (labor saved, spend on alternatives) x capture rate, held
> to ~20%/~5% vs 60-70% claims; invested $4.2M across 40 companies (18
> AI-driven), 7 with follow-on.

Two things are wrong and neither is a typo. It cannot be read — `held to
~20%/~5% vs 60-70% claims` means nothing to someone scanning for eight seconds.
And the *why* and the *impact* have been squeezed out to fit the character
budget.

The cause is structural. Compressing a true, detailed story into 250 characters
rewards `x`-joined noun phrases, `vs`, and tilde approximations; and a
recruiter-polish pass compresses what the rewrite already compressed. No amount
of "write clearly" in the prompt fixes an incentive.

So the model stopped writing sentences. It proposes word swaps and bullet
swaps, and the gates below decide.

---

## Gates on an edit (same bullet, reworded)

`check_text(new_text, original_text, db_bullet)` — returns `None` to accept, or
a reason string to reject. The reason reaches the report, which is how you tell
a correct rejection from a bug.

| gate | rule | constant |
|---|---|---|
| **word preservation** | the edit must keep 70% of the original's words | `MIN_WORD_OVERLAP = 0.7` |
| **no new numbers** | every figure in the edit must appear in the bullet's `text` or `context` | `_allowed_numbers()` |
| **no lost numbers** | losing even one figure from the original rejects the edit | — |
| **no length growth** | the edit may not be longer than the original | `MAX_LEN_GROWTH = 0` |
| **no punctuation growth** | it may not add commas, semicolons, parentheses or words | — |
| **no telegraphic notation** | `A x B x C`, `vs`, `~20%`, em dashes | `_SHORTHAND_PATTERNS` |
| **no borrowed nouns** | a proper noun or product name the original did not contain is rejected; acronyms are allowed | `_proper_nouns()` |
| **protected lines** | lines starting `Skills:` or `Languages:` are never touched | `PROTECTED_PREFIXES` |
| **total budget** | the sum of all bullets may exceed the original by at most 40 characters | `TOTAL_GROWTH_SLACK = 40` |

The priority order is deliberate, and it came from a human review of real
output: **(1)** keep quantified results, **(2)** stay concise, **(3)** stay at
the same level of abstraction. Hence "cannot get longer" and "cannot pull in a
proper noun that was not there".

## Gates on a swap (a different bullet from the database)

A swap is judged differently, and this is not an oversight. The reference for an
edit is a polished résumé sentence; the reference for a swap is a terse database
line, so "cannot get longer than the original" would reject everything —
measured once, four proposed swaps in a row were rejected for "word count
increased" and "lost a quantified result", leaving zero swaps.

So a swap is held to **your own résumé's shape for that section** instead:

| gate | rule | constant |
|---|---|---|
| **word preservation** | 45% of the database line's words | `MIN_WORD_OVERLAP_SWAP = 0.45` |
| **style caps** | at most 3 commas, 1 semicolon, 1 parenthesis | `SWAP_MAX_COMMAS`, `SWAP_MAX_SEMICOLONS`, `SWAP_MAX_PARENS` |
| **section shape** | no more words than that section's longest approved bullet, and no more characters | computed from `base_resume.yaml` |
| **at least one number** | a swap must keep at least one figure | — |
| **no duplicated experience** | a swap sharing two or more figures with a bullet that is *staying* is the same story twice | `_duplicate_of_kept()` |
| **retired is absolute** | `retired: true` cannot be offered as a candidate, and is refused if planned anyway | candidate list + `apply_plan` + a test |

The duplicate check has a subtlety worth keeping: the comparison is against
bullets that **remain** on the résumé, not the one being removed. Swapping to a
different angle on the same experience is fine; printing the same figures twice
is not. The measured failure was a swap that repeated "4,000 tickets / 18%" from
a bullet still on the page, and the recruiter review raised it as its top
objection.

## How many swaps

```
MIN_SWAPS = 1
MAX_SWAPS = 2
```

Every tailored résumé should differ from the baseline by one or two bullets. If
a plan yields zero valid swaps, the planner is asked **once** more with the
rejection reasons attached. If that also yields zero, the baseline ships and the
report says so explicitly.

It will not manufacture a swap to look busy. The reason the retry exists at all:
a run that ended with zero swaps was indistinguishable, from the report, from
one that had swapped successfully.

## One page

Two mechanisms, in order.

**Measurement.** `pagefit.py` drives Microsoft Word through AppleScript and asks
it for the page count and per-paragraph line counts. Estimating from character
counts is wrong at exactly the boundary that matters, which is the only place
the answer is interesting. Without Word the tool falls back to the estimate in
`validate.py` and says that it did.

**Reverting, not compressing.** When the draft spills, `revert_largest()` undoes
the biggest edit, then the next, then the swaps, until it fits. Since the
baseline is one page, reverting always converges. The earlier approach — asking
the model to shorten the overflowing bullet — is where the unreadable sentences
came from.

**Widow lines.** A bullet whose last line carries one or two words wastes a line
even at one page (`WIDOW_MAX_WORDS`). Those are shortened by
`shorten_deletion_only()`, where the model is allowed **only to delete words**:
`is_deletion_only()` checks the result against the original's word sequence, and
any lost figure rejects it. Deletion-only is the only form of model-written
compression anywhere in this project.

## Structural guards

The gates above judge sentences. These judge the document, and they exist
because a résumé can be wrong in ways no sentence check can see.

- **the rendered document must match the selection.** After writing the .docx,
  every chosen sentence must be in it (`run._verify_rendered`) and each section
  must hold exactly as many bullets as were selected
  (`run._verify_render_counts`). The failure that motivated this: a
  `base_resume.yaml` covering 2 of a document's 8 bullets produced a four-bullet
  résumé, and the page measurement reported it as six lines shorter than the
  baseline and called that a pass.
- **minimal mode never substitutes.** A section the result does not cover used to
  fall back to "the first bullet in the database". That is how content went
  missing quietly. It is now a `SetupError` naming the section.
- **the setup is validated before the first model call.**
  `run._validate_inputs` checks `base_resume.yaml` against both the database and
  the document — ids that do not exist, headings that do not match, sections it
  forgot. These used to surface at render time, after the paid work, or not at
  all.
- **a missing terminal is not an error.** Every prompt goes through `ask.py`,
  which returns the documented default instead of raising EOFError. A piped or
  scheduled run used to die at the confirmation step with the expensive calls
  already spent.

`python run.py doctor` runs the same checks on demand, and reports the
non-blocking ones too: a `profile.yaml` still holding the example's values, a
base that lists fewer bullets than the document, a sentence that has drifted
between the two files.

## The recruiter pass

`recruiter_review.py` scores the draft the way someone scanning for eight
seconds would, and lists objections.

By default in `minimal` mode it **only scores** (`recruiter_polish: false`). The
score and the objections go in the report; the wording stays yours. This is not
timidity: the polish step is precisely what produced the unreadable sentence at
the top of this document. Turn it on with `recruiter_polish: true` if you want
to see what it proposes, and read the diff carefully.

An objection the review raises twice is worth fixing by hand in
`resume_db.yaml`, not by letting the model rewrite the line. A reverted polish
leaves the objection standing.

## Tests

The suite is mostly pinned rejections: sentences a human judged bad, held in
place so the same kind of edit can never pass again.

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

They run on `tests/fixtures/`, not on your data, so they pass on a fresh clone.
When you add a validator, add the sentence that motivated it. That is what makes
this directory the valuable part of the repository rather than a pile of
thresholds.
