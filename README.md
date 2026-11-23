# resume-tailor

Finds job postings, screens them against what you actually want, and tailors
your one-page résumé to the ones worth applying to — by editing the résumé you
already approved, not by writing a new one.

Four stages, and each is useful on its own:

```
  Tracker            Database          JD analysis         Retailor
  ─────────          ────────          ───────────         ────────
  watch the     →    everything   →    what does      →    edit YOUR approved
  places                you              this posting         résumé: swap words,
  postings              could ever       ask for, and         replace one or two
  appear                say about        does it fit          bullets, keep it to
                        yourself         what you want?       one real page
```

It will not invent a number, rewrite your sentences into recruiter-speak, or
submit anything anywhere. When an edit cannot pass the checks, the original
sentence stays.

> **New here:**
>
> ```bash
> python run.py setup
> ```
>
> Six stages, every question skippable, and it only asks what your previous
> answers make relevant. [SETUP.md](SETUP.md) explains what it wrote and how to
> change any of it; `python run.py doctor` says what is still missing at any
> point.

## What you need, cost first

| | |
|---|---|
| **An LLM backend** | One of: the Claude Code CLI on a subscription, an Anthropic API key (metered), a local model via [Ollama](https://ollama.com) (free), or any agent CLI such as OpenAI Codex. See [Backends](#backends) — **you do not need all of them, and you do not need Ollama.** |
| **Python 3.10+** | 3.14 is what it is developed against. |
| **Your résumé, as a one-page .docx** | Everything is rendered from this file: its fonts, margins and bullet formatting are the output's. No such file yet? `templates/resume_template.docx` is one in the right shape, and setup offers it. |
| **An evening** | About 20 minutes of installing and answering setup, then an hour or two writing down your own experience in `resume_db.yaml` — that part is what determines output quality, and you keep adding to it. |
| *Optional:* macOS + Microsoft Word | Only for the page-count **guarantee**: Word is driven by AppleScript to measure the real page count. Without it you still get drafts and the page count becomes an estimate against your own template, which is usually right and can be wrong at the boundary. LibreOffice is not a substitute (no Calibri, so even a correct one-page file renders as two). |
| *Optional:* a job source | Trackly, a company's own Greenhouse/Lever/Ashby board, or Remotive — which needs no account at all. Without one, you hand it postings yourself. [tracker/CHANNELS.md](tracker/CHANNELS.md). |
| *Optional:* a notification channel | Telegram (the only one with ✅/⏭️ buttons), Discord, email — or nothing, in which case requests land in `state/inbox.md` and `run.py inbox` reads them. |

Nothing is sent anywhere except to the LLM backend you choose. With Ollama,
nothing leaves the machine at all. See [Privacy](#privacy).

## What it does, and what it does not

**Does**

- watches the places postings appear, and queues only the ones worth asking you about
- reads a posting from a URL or a text file, and extracts what it actually asks for
- tells you whether the posting's stated requirements admit you at all, quoting the posting
- judges fit *before* generating anything, so you are not handed twelve drafts you did not want
- screens on your own hard requirements — employment type, place, level, years, pay — in code, and names each mismatch instead of folding it into a score
- edits your approved résumé: wording to match the posting's vocabulary, one or two bullets swapped for closer experience
- enforces one page by measuring it, and by reverting edits rather than compressing sentences
- scores the draft the way a recruiter reading for eight seconds would, and reports the objections
- typesets a cover letter you wrote, in the same format as the résumé (`cover_letter.py` — the prose is yours; this is not a generator)

**Does not**

- submit an application, anywhere, ever. The closest it gets is sending the .docx to your own phone.
- invent numbers. A figure that appears in a rewritten sentence but not in your database is treated as fabrication and the edit is discarded.
- rewrite your sentences from scratch in the default mode. It swaps words and whole bullets; it does not re-author your voice.
- keep a worse version. Every gate's failure mode is "keep what you already approved".
- lose content. If the rendered document does not contain exactly what was selected, the run stops instead of handing you a shorter résumé.

## Quick start

```bash
git clone https://github.com/inho0333/resume-tailor-public.git resume-tailor
cd resume-tailor
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

.venv/bin/python run.py setup
```

Setup reads your .docx and writes the four files that describe you, asks what
you are looking for, checks any job source you pick against its real API, and
finishes by running `doctor`. Everything it asks can be skipped; everything it
writes can be edited by hand afterwards. Re-run one part with
`run.py setup --only <stage>`.

No résumé in the shape this expects? Setup offers
`templates/resume_template.docx` — a one-page layout with the `EXPERIENCE`
heading, `Company, City` header lines and the bulleted achievements the renderer
needs. Accept it to watch the whole pipeline run before your own career data is
involved.

Once set up:

```bash
# let it find postings, then read the queue and build one
.venv/bin/python run.py watch
.venv/bin/python run.py inbox
.venv/bin/python run.py pending --make <id>

# tailor for one posting
.venv/bin/python run.py tailor --url "https://..." --company "Acme" --role "Product Manager Intern"

# or from a saved posting, which is more reliable than scraping
.venv/bin/python run.py tailor --jd-file posting.txt --company "Acme" --role "Product Manager Intern"

# score an existing draft like a recruiter would
.venv/bin/python run.py review --company "Acme" --role "Product Manager Intern"

# check the setup, and optionally send one real prompt through each backend
.venv/bin/python run.py doctor --probe
```

Output lands in `output/<Company>/`.

## How it works

**(code)** means deterministic Python — no model involved, and therefore not
subject to a model's mood. **(light)** and **(heavy)** are the two LLM roles;
see [Backends](#backends).

### Stage 1 — Tracker: finding the postings

Optional, and separate from everything below: skip it and hand postings over
yourself. `run.py watch` polls the sources you chose and queues what survives.

| | step | |
|---|---|---|
| 1 | poll each source — an aggregator, a company's own Greenhouse/Lever/Ashby board, Apple's careers search, a public remote index | code |
| 2 | drop what has been seen before: by the source's own id, and by company + title across sources, because the same opening found twice has two unrelated ids | code |
| 3 | fill in a missing posting body from its URL, cheapest route first: the employer's ATS API, then the page's schema.org block, then its visible text | code |
| 4 | one short judgment — is this even the same kind of work? — against your profile, your experience and what you have approved before. Fails open: every error lets the posting through | light |

The first poll of a new source marks everything it can see as already seen and
sends nothing. Without that, turning on a source means one notification per
posting in its whole index — measured at around fifty.

### Stages 2–4 — Database, JD analysis, Retailor

| | step | |
|---|---|---|
| 1 | fetch the posting: its schema.org `JobPosting` block first (clean text plus the facts it states outright), then a readability extractor, then plain HTML | code |
| 2 | find sponsorship-exclusion language, and licence/clearance requirements, by regex — models read straight past both | code |
| 3 | judge whether the posting's degree requirements admit you, quoting the posting | light |
| 4 | extract what the posting wants: must-haves, responsibilities, the company's own vocabulary, **and** its location, employment type, level, years required and pay | light |
| 5 | count repeated terms for ATS keyword matching | code |
| 6 | check your own hard requirements against those facts — type, place, arrangement, years, level, pay. Arithmetic, not judgment, and each mismatch is named | code |
| 7 | judge fit — how much of this posting your experience can actually evidence — then clamp with your own target roles, your real past decisions, and the mismatches from step 6 | light |
| 8 | *(optional)* ask you to approve or skip, batched into a digest | code |
| 9 | research the company for red flags and for the words it uses about itself | heavy |
| 10 | plan the edit: which wording to swap, which one or two bullets to replace | heavy |
| 11 | apply the plan through the validators — **this is where most proposed edits die** | code |
| 12 | score the result as a recruiter, and report the objections | heavy |
| 13 | measure the page count in Word; if it spilled, revert edits (then swaps) until it fits | code |

Steps 1–8 are cheap: a local model does them for nothing, and **nothing past
step 8 runs until you approve**. Steps 9–12 are where model quality shows up in
what you send.

## The quality gates

This is the actual content of the project. Every rule below exists because a
model produced something worse without it — and the enforcement is in code,
because prompt instructions alone did not hold.

The failure that set the design: a pipeline that rewrote every bullet for every
posting produced lines like

> Built a bottom-up model at a $200M AUM firm sizing customers x willingness-to-pay
> (labor saved, spend on alternatives) x capture rate, held to ~20%/~5% vs 60-70%
> claims; invested $12.4M across 28 companies (9 AI-driven), 6 with follow-on.

Unreadable, and the *why* and the *impact* are gone. The cause is structural:
compressing facts into a character budget rewards telegraphic notation, and a
recruiter-polish step compresses it again. So the default mode stopped rewriting
sentences. Your approved résumé became the source of truth, and the model's job
shrank to word choice and bullet selection.

What the validators enforce, on every proposed edit:

- **word preservation** — an edited sentence must keep 70% of the original's words; a swapped-in bullet, 45%
- **no new numbers** — any figure not present in the bullet's `text` or `context` is fabrication; the edit is dropped
- **impact numbers survive** — losing even one figure from the original rejects the edit
- **no growth** — an edit may not get longer, or add commas, semicolons, parentheses or words
- **no telegraphic notation** — `A x B x C`, `vs`, `~20%`, em dashes
- **no borrowed nouns** — a proper noun or product name the original did not contain is rejected (acronyms are fine)
- **swap style caps** — a replacement bullet must stay inside your own résumé's shape for that section: at most 3 commas, 1 semicolon, 1 parenthesis, no more words than the section's longest bullet, and at least one number kept
- **no duplicated experience** — a bullet sharing two or more figures with one that is staying is the same story twice, and is rejected
- **a swap must bring in a different bullet** — swapping a bullet for itself would let a base sentence be rewritten under the looser swap rules, which is how one lost its *why* and *how* clauses and still passed every gate
- **plain hyphens only** — a non-breaking hyphen looks right in Word and breaks in a plain-text ATS field, so look-alike characters are normalised away before validation and rejected if they survive it
- **retired experience never returns** — a bullet marked `retired: true` cannot reach the résumé through any path, and a test holds that shut
- **protected lines** — a labelled line like `Skills:` is never reordered or reworded. Which labels those are comes from your own document (`protected_prefixes`), because the list was hardcoded to exactly two and a résumé reading `Technical Skills:` had no protection at all
- **one page, measured** — spillover is fixed by reverting edits, not by compressing sentences. Since your original fits on one page, reverting always converges.
- **widow removal is deletion-only** — when a bullet's last line holds two words, the model may only delete words; the result is checked against the original word order, and rejected if any number disappears

When a plan yields zero valid swaps, the tool asks once more, then ships your
résumé as-is and says so in the report. It will not manufacture a swap to look
busy.

## Backends

Two roles, because the work is not uniform:

- **light** — classification and extraction (steps 1–7). Frequent, cheap, fine on a small model.
- **heavy** — writing and judging résumé sentences (steps 8–11). A few calls per posting, and the whole quality of the result.

Configure them in `user_config.yaml` (`light_backend`, `heavy_backend`) or per
run with `--backend` / `--heavy-backend`. Both default to `auto`.

| backend | what it uses | cost | notes |
|---|---|---|---|
| `auto` | the best of what you have | — | light prefers a free local model; heavy prefers a frontier model and warns you when it has to fall back |
| `claude-cli` | `claude -p` on the logged-in Claude Code CLI | subscription | the rate limit is **shared with your own interactive Claude Code**; a window you exhausted by hand fails these calls too |
| `claude-api` | Anthropic API, `ANTHROPIC_API_KEY` | metered per token | no CLI needed |
| `ollama` | a local model (`gpt-oss:20b` by default) | free | nothing leaves the machine; weaker at prose |
| `codex-cli` | `codex exec`, or any agent CLI you define | yours | argv is configurable in `user_config.yaml`; verify with `doctor --probe` |

**Choosing:** on a desktop that is always on, Ollama for light work costs
nothing and keeps your data local. On a laptop, leave both on `auto` and let it
use whichever CLI you have — a 20B local model is slow on battery and visibly
weaker at the one step where quality matters. Run `python run.py doctor` to see
what this machine actually offers.

## Data model

Four files describe you. All four are gitignored; each has a committed `.example`
twin whose comments are the real documentation.

| file | what it holds | how you make it |
|---|---|---|
| `profile.yaml` | who you are (background, education, work authorisation) and **what you are looking for** — type of role, target and avoided roles, level, places, arrangement, pay floor, dealbreakers — plus two or three of your own bullets as a style reference | `run.py setup` |
| `user_config.yaml` | paths, mode, per-section bullet caps, backend choice, job sources, notification channels | `run.py setup`, then edit |
| `resume_db.yaml` | **every** bullet you could ever use, with the context and the allowed numbers behind each one | `run.py setup` reads your .docx, then hours of your own writing |
| `base_resume.yaml` | which bullets your approved résumé actually shows, keyed to the headings in the .docx | `run.py setup` writes it from your document; you review it once |

Every axis in `profile.yaml`'s `search` block is optional, and **empty has a
precise meaning: that axis is not screened at all.** It is never a hidden
filter. See [docs/preferences.md](docs/preferences.md).

A fifth file is committed, and is not about you: `templates/resume_template.docx`,
a standard one-page layout in the shape the parser and the renderer expect. Start
from it if your own résumé is written as paragraphs, or point `bootstrap` at it to
watch a full run before your own career data is involved.

`resume_db.yaml` is where output quality comes from. Each bullet's `context`
field is both the background the model rewrites from and **the list of numbers it
is allowed to use** — a figure that is not there cannot appear in your résumé.
Write down every figure you might ever want, even those the current sentence
does not use.

## Privacy

- Your résumé, your experience database, your profile, every draft and every
  pending posting are gitignored. The repository contains code and schema
  examples only.
- Posting text and your bullets go to the backend you chose, and nowhere else.
  With `ollama`, nothing leaves the machine.
- Secrets come from the environment: `ANTHROPIC_API_KEY`, `TG_BOT_TOKEN`,
  `TG_CHAT_ID`. No file in this repo holds a credential, and none should.
- `state/` keeps a journal of what you approved and skipped, locally, and the
  fit judgment uses it as examples of your taste.

## Tests

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

They run on fixtures in `tests/fixtures/`, not on your data, so they pass on a
fresh clone. Most of them are pinned rejections: a sentence a human judged bad
once, held in place so the same kind of edit can never pass again.

## Documentation

- [SETUP.md](SETUP.md) — zero to a first tailored résumé
- [docs/architecture.md](docs/architecture.md) — modules, data flow, where to change what
- [docs/quality-gates.md](docs/quality-gates.md) — every validator, what it rejects, why it exists
- [docs/operations.md](docs/operations.md) — running it unattended, the Telegram approval loop, troubleshooting
- [docs/tracker.md](docs/tracker.md) — the source interface, and how to add one
- [tracker/CHANNELS.md](tracker/CHANNELS.md) — every job source: coverage, setup cost, limits
- [docs/preferences.md](docs/preferences.md) — every screening axis and what skipping it means
- [docs/cover-letter.md](docs/cover-letter.md) — the cover-letter formula

## Status

Built for one person's job search, then generalised. Expect rough edges:

- macOS-only for the page-count guarantee (Word via AppleScript). Everything
  else is cross-platform, but the measurement fallback is an estimate.
- `bootstrap` parses a résumé with an `EXPERIENCE` section heading and
  `Company, City` header lines, with bulleted achievements under each. A different
  layout will need the parser adjusted; `templates/resume_template.docx` is a
  document in the expected shape.
- every `header_text` in `base_resume.yaml` must match its .docx paragraph
  character for character. Bootstrap gets this right; a document edited
  afterwards can drift, which is what `doctor` checks.
- The unattended loop (`watch` under launchd or cron, plus a notification
  channel) is the least portable part. `run.py inbox` is the version that needs
  nothing.
- `tracker/sources/linkedin.py` is deliberately an interface with no
  implementation; the file says why, and what to use instead.

MIT licensed. Issues and patches welcome.
