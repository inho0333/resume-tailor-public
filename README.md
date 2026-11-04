# resume-tailor

Tailors your one-page résumé to a specific job posting, and refuses to do it
badly. Give it a posting; it decides whether the posting is worth your time,
edits the wording of the résumé you already approved, swaps one or two bullets
for better-matching experience, and keeps the result to one real page — measured
in Word, not estimated.

It will not invent a number, rewrite your sentences into recruiter-speak, or
submit anything anywhere. When an edit cannot pass the checks, the original
sentence stays.

> **New here? Go to [SETUP.md](SETUP.md).** It takes about 40 minutes, and
> `python run.py doctor` tells you what is still missing at any point.

## What you need, cost first

| | |
|---|---|
| **An LLM backend** | One of: the Claude Code CLI on a subscription, an Anthropic API key (metered), a local model via [Ollama](https://ollama.com) (free), or any agent CLI such as OpenAI Codex. See [Backends](#backends) — **you do not need all of them, and you do not need Ollama.** |
| **macOS + Microsoft Word** | Only for the page-count guarantee. Word is driven by AppleScript to measure the real page count. Without it you still get drafts; the page count becomes an estimate that can be wrong at the boundary. LibreOffice is not a substitute (no Calibri, so even a correct one-page file renders as two). |
| **Python 3.10+** | 3.14 is what it is developed against. |
| **Your résumé, as a one-page .docx** | Everything is rendered from this file: its fonts, margins and bullet formatting are the output's. |
| **About 40 minutes of setup** | Most of it writing down your own experience in `resume_db.yaml`, which is the part that determines output quality. |
| *Optional:* a Telegram bot | Only to approve or skip postings from your phone. The CLI works without it. |

Nothing is sent anywhere except to the LLM backend you choose. With Ollama,
nothing leaves the machine at all. See [Privacy](#privacy).

## What it does, and what it does not

**Does**

- reads a posting from a URL or a text file, and extracts what it actually asks for
- tells you whether the posting's stated requirements admit you at all, quoting the posting
- judges fit *before* generating anything, so you are not handed twelve drafts you did not want
- edits your approved résumé: wording to match the posting's vocabulary, one or two bullets swapped for closer experience
- enforces one page by measuring it, and by reverting edits rather than compressing sentences
- scores the draft the way a recruiter reading for eight seconds would, and reports the objections
- writes a cover letter draft from the same material

**Does not**

- submit an application, anywhere, ever. The closest it gets is sending the .docx to your own phone.
- invent numbers. A figure that appears in a rewritten sentence but not in your database is treated as fabrication and the edit is discarded.
- rewrite your sentences from scratch in the default mode. It swaps words and whole bullets; it does not re-author your voice.
- keep a worse version. Every gate's failure mode is "keep what you already approved".
- lose content. If the rendered document does not contain exactly what was selected, the run stops instead of handing you a shorter résumé.

## Quick start

```bash
git clone <this repo> && cd resume-tailor
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

cp profile.example.yaml profile.yaml          # who you are
cp user_config.example.yaml user_config.yaml  # where your files are
.venv/bin/python run.py doctor                # what is still missing
```

Then follow [SETUP.md](SETUP.md), which walks through the two files above and
generates `resume_db.yaml` and `base_resume.yaml` from your .docx.

Once set up:

```bash
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

Twelve steps. **(code)** means deterministic Python — no model involved, and
therefore not subject to a model's mood. **(light)** and **(heavy)** are the two
LLM roles; see [Backends](#backends).

| | step | |
|---|---|---|
| 1 | fetch the posting text from a URL, with a readability extractor and a plain-HTML fallback | code |
| 2 | find sponsorship-exclusion language by regex, because models read straight past it | code |
| 3 | judge whether the posting's degree requirements admit you, quoting the posting | light |
| 4 | extract what the posting wants: must-haves, responsibilities, the company's own vocabulary | light |
| 5 | count repeated terms for ATS keyword matching | code |
| 6 | judge fit — how much of this posting your experience can actually evidence — and clamp the verdict with your own `target_roles` and your real past decisions | light |
| 7 | *(optional)* ask you to approve or skip, batched into a digest | code |
| 8 | research the company for red flags and for the words it uses about itself | heavy |
| 9 | plan the edit: which wording to swap, which one or two bullets to replace | heavy |
| 10 | apply the plan through the validators — **this is where most proposed edits die** | code |
| 11 | score the result as a recruiter, and report the objections | heavy |
| 12 | measure the page count in Word; if it spilled, revert edits (then swaps) until it fits | code |

Steps 1–7 are cheap: a local model does them for nothing. Steps 8–11 are where
model quality shows up in what you send.

## The quality gates

This is the actual content of the project. Every rule below exists because a
model produced something worse without it — and the enforcement is in code,
because prompt instructions alone did not hold.

The failure that set the design: a pipeline that rewrote every bullet for every
posting produced lines like

> Built a bottom-up model at a $500M AUM firm sizing customers x willingness-to-pay
> (labor saved, spend on alternatives) x capture rate, held to ~20%/~5% vs 60-70%
> claims; invested $4.2M across 40 companies (18 AI-driven), 7 with follow-on.

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
- **retired experience never returns** — a bullet marked `retired: true` cannot reach the résumé through any path, and a test holds that shut
- **protected lines** — the Skills and Languages lines are never reordered or reworded
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
| `profile.yaml` | degree, graduation, work authorisation, target roles, and two or three of your own bullets as a style reference | copy `profile.example.yaml` |
| `user_config.yaml` | paths, mode, per-section bullet caps, backend choice | `run.py bootstrap`, then edit |
| `resume_db.yaml` | **every** bullet you could ever use, with the context and the allowed numbers behind each one | `run.py bootstrap --resume your.docx`, then hours of your own writing |
| `base_resume.yaml` | which bullets your approved résumé actually shows, keyed to the headings in the .docx | `run.py bootstrap` writes it from your document; you review it once |

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
- [docs/cover-letter.md](docs/cover-letter.md) — the cover-letter formula

## Status

Built for one person's job search, then generalised. Expect rough edges:

- macOS-only for the page-count guarantee (Word via AppleScript). Everything
  else is cross-platform, but the measurement fallback is an estimate.
- `bootstrap` parses a résumé with `EXPERIENCE` section headings and
  `Company, City` header lines. A different layout will need the parser adjusted.
- every `header_text` in `base_resume.yaml` must match its .docx paragraph
  character for character. Bootstrap gets this right; a document edited
  afterwards can drift, which is what `doctor` checks.
- The unattended approval loop (Telegram + launchd) is the least portable part.

MIT licensed. Issues and patches welcome.
