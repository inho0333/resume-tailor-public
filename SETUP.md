# Setup

```bash
git clone https://github.com/inho0333/resume-tailor-public.git resume-tailor
cd resume-tailor
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

.venv/bin/python run.py setup
```

That is the whole thing. The wizard asks only what your previous answers make
relevant, every question can be skipped, and it finishes by running `doctor`.

This file is what it wrote, why, and how to change any of it afterwards. You do
not have to read it to get started.

> Setup used to be eleven manual steps across three hand-edited files, in an
> order that mattered and was only implied. The two mistakes it produced most
> were silent: a profile left at the example's values, so every judgment was
> confidently about a fictional person; and a target-role list nobody edited, so
> a stranger's job search was applied to every posting. The order is code now.

**Re-run one part at any time:**

```bash
.venv/bin/python run.py setup --only environment     # which model does what
.venv/bin/python run.py setup --only resume          # re-read your .docx (first time)
.venv/bin/python run.py setup --only identity        # who you are
.venv/bin/python run.py setup --only search          # what you are looking for
.venv/bin/python run.py setup --only sources         # where postings come from
.venv/bin/python run.py setup --only notifications   # how you are asked
```

---

## 1. Which model does what

Two roles, because the work is not uniform:

- **light** — reading postings, judging fit. Runs many times per posting. A
  local model is fine.
- **heavy** — writing the sentences you submit. A few calls per draft, and this
  is where quality shows. A frontier model is strongly preferred.

Both default to `auto`, which resolves each role separately from what the machine
actually has. Written to `user_config.yaml` as `light_backend` / `heavy_backend`;
override for one run with `--backend` / `--heavy-backend`.

If nothing was available, the cheapest paths in, in order:

```bash
brew install ollama && ollama serve & && ollama pull gpt-oss:20b   # free, local
export ANTHROPIC_API_KEY=sk-ant-...                                # best quality, metered
# or install the Claude Code CLI and log in                        # a subscription you may have
```

This stage runs first for a reason: the next one makes a model call while reading
your résumé.

### The one-page measurement

With Microsoft Word on macOS, the page count is **measured** by driving Word
through AppleScript. Run this once:

```bash
.venv/bin/python pagefit.py --grant
```

Word is sandboxed and puts up a file-access dialog the first time a script opens
a path. It waits for a human click — so in an unattended run, the whole pipeline
stalls there. Granting once, against a fixed probe path, settles it permanently.

Without Word the count is estimated against your own template, which is usually
right and can be wrong at the boundary. **LibreOffice is not used for this**,
even when installed: without Calibri it renders a correct one-pager as two
pages, which is worse than the estimate.

---

## 2. Your résumé

One document produces three files: `resume_db.yaml` (everything you could ever
say), `base_resume.yaml` (what your approved résumé actually says — the quality
floor), and the paths and per-section bullet caps in `user_config.yaml`.

The .docx is the template *and* the baseline. Everything is rendered back into
it, so its layout is the output's layout. It must:

- **be one page already.** One page is kept by reverting edits, which only
  converges if the original fits.
- **have an `EXPERIENCE` section heading** (setup asks, and accepts yours), with
  each employer as a `Company, City` header line followed by its bullets.
- **have bulleted achievements**, not paragraphs. A bullet is a paragraph with
  Word's bullet formatting or a `List Bullet` style; prose under a heading
  parses as zero experience blocks.
- **have the fonts you want.** Output inherits them. Calibri is what the page
  measurement is calibrated against.

Keep it somewhere stable — the path goes in `user_config.yaml`.

### No résumé in that shape?

Setup offers `templates/resume_template.docx`: a standard one-page layout —
centred name, a contact line, 12pt bold section headings, each employer as
`Company, City` with dates on a right tab stop, a one-line description of the
employer, the title and date range, then bulleted achievements. Accept it to
watch the whole pipeline work on a fictional person before your own career data
is involved.

`python templates/make_template.py` regenerates it; that script is where the
layout is defined, and its header explains each decision.

### Protected lines

Setup finds the labelled lines in your document — `Skills:`, `Languages:`,
`Technical Skills:`, whatever yours are — and writes them to
`protected_prefixes` in `user_config.yaml`. Those lines are excluded from
editing, from bullet swaps and from automatic shortening, because a Skills line
has a settled order and composition and shortening it is the easiest way for a
model to win back a line.

This list was hardcoded as exactly `("Skills:", "Languages:")` until it became a
setting, so a résumé reading `Technical Skills:` had no protection at all.
`doctor` warns about any labelled line the list does not cover.

### When you get a newer résumé

Do **not** re-run `bootstrap` or `setup --only resume`. Both write the database
from scratch, which is right exactly once — pointed at a second document they
throw away the `context` you wrote, every `retired` flag, and the ids that
`base_resume.yaml` references.

```bash
.venv/bin/python run.py import --resume ~/Desktop/Resume_2026.docx --dry-run
.venv/bin/python run.py import --resume ~/Desktop/Resume_2026.docx
```

`import` only ever adds. The database holds every bullet you could ever use and
a résumé shows a subset, so a bullet missing from the new document is kept. The
only questions it asks are about sentences that look like a reworded version of
one already there, and keeping the existing sentence is the default.

Add `--set-base` when the new document is also your new *approved* résumé: it
rewrites `base_resume.yaml` from it and repoints `resume_path`.

This is also the quickest way to fill the database in the first place. Writing
`context` by hand is the slow part; a longer or older résumé you still have
lying around is a few more bullets for the price of one command.

### Then fill in `resume_db.yaml`

This is the part that determines output quality, and the part nothing can do for
you. Budget an hour or two.

Each bullet's `context` field is both the background a rewrite works from and
**the list of numbers the model is allowed to use** — a figure that is not in
`text` or `context` cannot appear in your résumé, and an edit that introduces
one is discarded. Write down every figure you might ever want, including ones
the current sentence does not use.

`resume_db.example.yaml`'s comments are the real documentation for the schema.

### And check `base_resume.yaml` once

Setup writes it from your document, so it starts out as an exact copy of what
your résumé shows today. Two things have to hold, and `doctor` checks both:

- every `id` exists in `resume_db.yaml`
- every `header_text` matches its paragraph in the .docx **character for
  character** — a mismatch is a hard error, not a warning

The same sentence deliberately lives in three places — the database, this file,
and the document — so drift between them is detectable.

---

## 3. Who you are — `profile.yaml`

Every judgment this tool makes is about a person, and the failure mode is quiet:
a profile left at the example's values produces a complete, plausible,
confidently-reported résumé for somebody who does not exist. So four fields are
blocking in `doctor` — your name, your summary, your employers and your target
roles.

What setup asks depends on your answer to the first question:

| you are | it asks | it does not ask |
|---|---|---|
| a student now | school, degree, graduation, technical or not | years of experience |
| recently graduated | the same, marked complete | years of experience |
| years into a career | years of experience, and a degree only if you want one considered | a graduation date |
| changing field | the same, plus the prompts weigh transferable evidence | — |

That branch exists because the first version described everyone as a student,
down to a literal "a graduate student" for a profile with no programme — and
`doctor` asked an experienced hire to fill in a degree to clear a warning, which
then told the recruiter prompt that the pile they were being compared against
was made of students.

**Work authorisation** is one sentence in a recruiter's words, and leaving it
empty means no prompt reasons about your status at all. The sponsorship scan
still runs either way: it reads the posting, not you.

**`voice.style_examples`** is two or three of your own strongest bullets,
verbatim. The model is shown them as "this is what a good bullet looks like
here" before it edits anything — they set sentence length, how much context a
bullet carries and how numbers are written. The highest-leverage field in the
file. Left empty, the prompts fall back to fictional examples: safe, and weaker.

`profile.example.yaml`'s comments document every field.

---

## 4. What you are looking for

The axes a posting is screened on: employment type, target and avoided roles,
level, places, arrangement, relocation, pay floor, dealbreakers.

**Every one is optional, and empty means that axis is not screened at all.** Not
screened loosely, not screened with a guess. It is never a hidden filter.

The ones that are set are checked in code rather than by a model — a location
mismatch is arithmetic — and each mismatch is named in the digest instead of
being folded into a score.

[docs/preferences.md](docs/preferences.md) covers each axis, what it changes, and
what skipping it means. Worth ten minutes: it is also where the reasoning behind
"strong fit" lives, which is the number the digest sorts on.

---

## 5. Where postings come from

Optional. Without it, postings arrive the way they always did:

```bash
.venv/bin/python run.py tailor --url "https://..."
```

With it, `run.py watch` finds them:

```bash
.venv/bin/python run.py watch        # one tick
.venv/bin/python run.py inbox        # read the queue
```

Setup offers each source with what it covers and what it costs, then makes a
real request against whatever you pick. That check matters: a source that cannot
run returns nothing, and nothing is indistinguishable from a quiet day — one
placeholder board token was polled every two minutes for a week, answered 404
every time, and was reported throughout as "no new postings".

[tracker/CHANNELS.md](tracker/CHANNELS.md) is the full list: Trackly, a
company's own Greenhouse/Lever/Ashby board, Apple's careers search, Remotive
(which needs no account at all), and why LinkedIn is deliberately not
implemented.

The first poll of a new source marks everything it can see as already seen and
sends nothing, so turning one on does not fire one notification per posting in
its whole index.

To run it on a schedule, see the end of [docs/tracker.md](docs/tracker.md) —
including the launchd `PATH` trap, which breaks a Node-based CLI only under the
scheduler.

---

## 6. How you are asked

**No channel is required.** With none configured, approval requests are written
to `state/inbox.md` and answered from the terminal:

```bash
.venv/bin/python run.py inbox                   # the whole queue, readably
.venv/bin/python run.py pending --make <id>     # build this one
.venv/bin/python run.py pending --skip <id>     # pass on it
```

The channels, in the order setup offers them:

| | setup cost | buttons |
|---|---|---|
| **telegram** | ~5 min with @BotFather | ✅/⏭️, answered in the chat |
| **discord** | one webhook URL | no — you answer from the CLI |
| **email** | your own SMTP credentials | no |
| **local** | none, and the default | no |
| **whatsapp** | interface only; `channels/whatsapp.py` says why | — |

Telegram is the only one that can answer its own messages, which is why it is
first. Four steps:

1. message **@BotFather** on Telegram, send `/newbot`
2. `export TG_BOT_TOKEN=<the token it gives you>`
3. send your new bot any message, open
   `https://api.telegram.org/bot$TG_BOT_TOKEN/getUpdates`, read
   `result[0].message.chat.id`
4. `export TG_CHAT_ID=<that id>`, then run `python bot_listener.py`

Put both exports in your shell profile so a scheduled run sees them. There is a
launchd plist for the listener at `com.local.confirm-bot-listener.plist.example`
— note that it stores the token in plaintext, so do not back it up anywhere
shared.

### Batching

Requests are batched into one digest at `digest_hours` (9 and 20 by default).
This is not a cosmetic choice: sending one message per posting produced 15 in a
single day, 11 of them inside two and a half hours, and 13 answers out of 87
requests. Batched, each posting keeps its own ✅/⏭️ pair on its own row, and the
digest is sorted best-fit first.

An unanswered request expires after `confirm_ttl_days`. Each one holds a whole
job posting, and a three-week-old posting has usually closed.

---

## First run

```bash
.venv/bin/python run.py doctor
.venv/bin/python run.py tailor --jd-file posting.txt --company "Acme" --role "Product Manager"
```

`--jd-file` for the first run rather than `--url`: saving the posting text
yourself removes every extraction failure from the picture, so if something goes
wrong you know it is not the fetch.

Output lands in `output/<Company>/` — the .docx, the posting analysis, the
recruiter review, and a report naming every edit that was rejected and which
rule it hit.

**Most proposed edits being rejected is the expected result**, not a
malfunction. It is the design: when an edit cannot pass, the sentence you
already approved is what ships.

---

## Troubleshooting

| symptom | cause | fix |
|---|---|---|
| `profile.yaml not found` | setup not run, or the identity stage skipped | `run.py setup --only identity` |
| `No LLM backend is available` | no CLI, no key, no local model | `run.py setup --only environment`; `doctor` lists every option |
| `FileNotFoundError` on a .docx | `resume_path` / `base_resume_path` wrong or moved | fix `user_config.yaml`, then `doctor` |
| minimal mode refuses to run | `base_resume.yaml` missing | `run.py setup --only resume` |
| `base_resume.yaml does not match your résumé` | a heading differs by a character, an id does not exist, or a section is missing | `doctor` lists each with its fix; the run stops before spending anything |
| `the rendered document does not match what was selected` | the template's headings or bullet formatting changed under the database | `doctor`, then check the headings in the .docx |
| every proposed edit is rejected | the model is proposing rewrites, not edits | expected, and the point. The report says which rule each one hit; your original ships |
| zero swaps, and the report says so | nothing in the database matched this posting better | add the missing experience to `resume_db.yaml` |
| page count "could not be measured" | no Word, or the access dialog is waiting | `pagefit.py --grant`, or accept the estimate |
| rate-limit message on `claude-cli` | the subscription window is shared with your own interactive sessions | wait for the reset it names, or switch backend for that run |
| the extracted posting is boilerplate | a JavaScript-rendered job board | save the text and use `--jd-file` |
| Telegram buttons do nothing | `bot_listener.py` is not running | start it, or use `run.py pending --make <id>` |
| `--auto` produced no draft | that is the approval gate, not a failure | `run.py inbox` lists it; `pending --make` builds it |
| `profile.yaml still has profile.example.yaml's values` | the identity or search stage was skipped | edit the fields the message names, or `setup --only identity` |
| `found no experience blocks` | the résumé is prose, or has no `EXPERIENCE` heading | section 2 above; compare against `templates/resume_template.docx` |
| `watch` says a source cannot run | a wrong board token, or a missing credential | the message names the fix; `doctor` re-checks every source |
| `watch` finds nothing, ever | the first poll baselined the source; or its own filters are too narrow | normal on run one. Otherwise widen the source's settings — for Trackly, on their website, not here |
| postings arrive that are obviously wrong | the relevance filter fails open on purpose | narrow `search.target_roles`, or add to `tracker.block_titles` |

Still stuck: `doctor` first, then `doctor --probe`, then the report file in
`output/<Company>/`, which records every rejection with its reason.

See [docs/operations.md](docs/operations.md) for running it unattended and
[docs/tracker.md](docs/tracker.md) for the tracker's internals.
