# Setup

Zero to a first tailored résumé. Steps 1-6 take about half an hour; step 7 —
writing down your own experience — is an hour or two and is the part that decides
how good the output is. You can run the whole thing before step 7 and see what
comes out.

At any point:

```bash
.venv/bin/python run.py doctor
```

prints a checklist of what is set up, what is missing, and the exact command or
edit that fixes it. Every item on it was once a silent degradation rather than
an error, which is why it exists.

---

## 1. Install

```bash
git clone https://github.com/inho0333/resume-tailor-public.git resume-tailor
cd resume-tailor
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Python 3.10 or newer (3.14 is what it is developed against). Every command below
uses `.venv/bin/python` explicitly — if you activate the venv instead, plain
`python` is fine.

Check:

```bash
.venv/bin/python run.py doctor
```

The "Python and packages" block should be all `ok`. Everything under "Your
files" will fail; that is the rest of this document.

---

## 2. Pick an LLM backend

You need **one**. Not all of them, and **not Ollama specifically**.

### Option A — Claude Code CLI (a Claude subscription)

Best quality for the step that matters, nothing to configure.

```bash
# install Claude Code, then log in once:
claude
# say hello, confirm it answers, quit
```

Then nothing else: `auto` will find it. One thing to know — these calls are
billed to your subscription and share its rate limit with your own interactive
Claude Code sessions. If you exhaust the window by hand, a tailor run in that
window fails with a rate-limit message and no work is lost.

### Option B — Anthropic API key (metered)

```bash
export ANTHROPIC_API_KEY=sk-ant-...          # put it in your shell profile
```

Pay per token. A posting costs a few cents. Never put the key in a file in this
repo; the backend only reads it from the environment.

### Option C — a local model with Ollama (free)

```bash
brew install ollama        # or https://ollama.com
ollama serve &             # leave it running
ollama pull gpt-oss:20b
```

Free, and nothing leaves your machine. Good for the classification steps. It is
visibly weaker at writing bullets, so if you *only* have Ollama, the tool will
use it for that too and tell you it is doing so.

Rule of thumb: on a desktop that is always on, Ollama for the light steps costs
nothing. On a laptop, use a CLI or the API — a 20B model on battery is slow and
weaker exactly where it counts.

### Option D — OpenAI Codex, or any other agent CLI

Any CLI that takes a prompt and prints an answer. `codex-cli` and `gemini-cli`
have built-in defaults; override or add your own in `user_config.yaml`:

```yaml
backends:
  codex-cli:
    command: [codex, exec, --skip-git-repo-check, "-"]
    prompt_via: stdin        # or "arg" to pass the prompt as the last argument
```

These CLIs change their flags, so the argv is configuration, not code. Verify
yours with a real call:

```bash
.venv/bin/python run.py doctor --probe --backend codex-cli
```

### Check

```bash
.venv/bin/python run.py doctor
```

Under "LLM backends", at least one line should say `available`, and both
`auto (light)` and `auto (heavy)` should resolve. Then make one real call:

```bash
.venv/bin/python run.py doctor --probe
```

---

## 3. `profile.yaml` — who you are

```bash
cp profile.example.yaml profile.yaml
```

Open it and replace every value. `name`, `education.school`, `career_summary` and
`employers` are **blocking**: left at the example's values, `tailor` refuses to run
and `doctor` reports a FAIL, because every judgment would be about a person who
does not exist. The rest (`style_examples`, `target_roles`) only warn — they cost
quality, not correctness.

The comments in the file say what each field is for and what goes wrong when it
is wrong; the ones that matter most:

- **`education.program`** — written the way postings write it (`MBA`, `MS
  Computer Science`). This string goes into the eligibility prompt verbatim.
- **`education.graduation`** — `YYYY-MM`. Postings gate on class year, and this
  is what they are compared against. A year out silently mis-screens everything.
- **`education.degree_is_technical`** — flips how "requires a technical degree"
  is read.
- **`work_authorization`** — one sentence, in recruiter terms. **Leave it empty
  (`""`) if you would rather the prompts never reason about your status**; the
  sponsorship check that still runs is a regex over the posting, not a model
  judgment, so an empty value simply means no filtering.
- **`target_roles`** — the roles you actually want, as they appear in job titles,
  lowercase. These do real work: a posting matching one of them can never be
  tiered "low" by a small model, and "high" is only given when the model and the
  title agree.
- **`style_examples`** — two or three of your own strongest bullets, verbatim.
  The model is shown these as "this is what a good bullet looks like here"
  before it touches anything. Leave it empty and fictional examples are used
  instead, which is safe but weaker. Never paste someone else's bullets: their
  numbers leak into your output.

---

## 4. Get your résumé into the right shape

The .docx you point this at is the template *and* the quality baseline. Everything
is rendered back into it, so its layout is the output's layout. It must:

- **be one page already.** The tool keeps you at one page by reverting its own
  edits, which only converges if the original fits.
- **have an `EXPERIENCE` section heading** (or pass yours with
  `--section-heading`), with each employer as a `Company, City` header line
  followed by its bullets.
- **have bulleted achievements**, not paragraphs. A bullet is a paragraph with
  Word's bullet formatting or a `List Bullet` style; prose under a heading parses
  as zero experience blocks.
- **have the fonts you want.** Output inherits them. Calibri is what the page
  measurement is calibrated against.

Copy it somewhere stable — the path goes in `user_config.yaml` and should not
move.

### No résumé in that shape?

`templates/resume_template.docx` is one: a standard one-page business-school
layout — centred name, a contact line, 12pt bold section headings, each employer
as `Company, City` with its dates on a right tab stop, a one-line description of
the employer, the title and date range, then bulleted achievements.

```bash
# see the whole pipeline work on the fictional person in the template first
.venv/bin/python run.py bootstrap --resume templates/resume_template.docx
```

Then either replace its content with your own (keeping the paragraph shapes) or
reshape your existing résumé to match it. `python templates/make_template.py`
regenerates the file if you want to change the layout itself — that script is
where the layout is defined, and its header explains each decision.

---

## 5. `user_config.yaml` — paths and modes

You can skip this step: `bootstrap` (next) writes the file for you, pointing both
paths at the document you give it. Do it only if you want to change something
first, or read it afterwards to see what bootstrap chose.

```bash
cp user_config.example.yaml user_config.yaml
```

The three keys that matter:

```yaml
resume_path: /Users/you/Documents/Your_Resume.docx
base_resume_path: /Users/you/Documents/Your_Resume.docx   # usually the same file
tailor_mode: minimal
```

`tailor_mode: minimal` is the default and the mode worth using.

Editing this before bootstrap is safe: bootstrap only sets `resume_path` and
`max_bullets_per_section`, keeps every other key you wrote, and saves the
previous file as `user_config.yaml.bak`.

---

## 6. `bootstrap` — generate the experience database

```bash
.venv/bin/python run.py bootstrap --resume /Users/you/Documents/Your_Resume.docx
```

This parses your .docx and writes three files:

- **`resume_db.yaml`** — one entry per bullet it found, with `id`, `text` and
  rough tags
- **`base_resume.yaml`** — the approved résumé as data: every bullet your
  document shows, with the headings copied out of it exactly (step 8 is now a
  review rather than an hour of transcription)
- **`user_config.yaml`** — `resume_path` plus the per-section bullet caps it
  counted (those counts are what made your résumé fit on one page), merged into
  whatever you had already set

An existing `base_resume.yaml` is never overwritten: it records which bullets you
consider approved, which bootstrap cannot know.

If it cannot find your sections, it says so: check that the heading paragraph is
exactly `EXPERIENCE`, or pass `--section-heading "WORK EXPERIENCE"`.

---

## 7. Fill in `resume_db.yaml` — the part that matters

Bootstrap gives you sentences. What makes the output good is everything it
cannot know. Read `resume_db.example.yaml` alongside your file: its comments are
the documentation, field by field.

The field to take seriously is **`context`**. It is both the background the model
rewrites from and **the list of numbers the model is allowed to use**. A figure
that appears in a rewritten sentence but not in `text` or `context` is treated
as fabrication and the edit is thrown away.

So write down every figure you might ever want — team size, timeline, the
denominator, the before-and-after, the thing you decided against — even when
today's sentence uses none of them.

Also worth doing now:

- **add the bullets that are not on your résumé.** The database is meant to hold
  everything you *could* say. Bullet swapping has nothing to choose from if it
  only holds what is already there.
- **tag honestly.** `keywords` and `content_type` drive selection.
- **mark dead experience `retired: true`.** It then cannot reach a résumé by any
  path; a test holds that shut.

Budget an hour or two. This is the file you will come back to after every
interview that surfaces a story you had not written down.

---

## 8. `base_resume.yaml` — review what bootstrap wrote

Minimal mode renders from this file, so it decides what your tailored résumé
contains. Bootstrap wrote it from your document, which means it starts out
correct; open it once and **remove any line you do not want treated as
approved**.

Two rules hold it together, and both are hard errors:

1. every `id` must exist in `resume_db.yaml`
2. every `header_text` must match the heading paragraph in your .docx
   **character for character** — same spelling, punctuation, capitalisation.
   The renderer finds the section by string match.

The sentence under each `id` should stay identical to the `text` in
`resume_db.yaml` and to what is in the .docx. The same sentence in three places
is deliberate: it is how drift gets detected.

Check:

```bash
.venv/bin/python run.py doctor
```

`doctor` verifies both rules against your actual document and database — a
mismatched heading, an id that does not exist, a section the file forgot, a
sentence that has drifted. It also tells you when the file lists fewer bullets
than the document, which is allowed but means your tailored résumé will be
shorter than the one you approved. "Your files" should now be all `ok`.

---

## 9. Let Word be measured (macOS, one time)

The page-count guarantee works by driving Microsoft Word with AppleScript. The
first time a script opens a file, macOS shows a "Grant Access" dialog — and an
unattended run would sit there waiting for a click forever. Get it over with:

```bash
.venv/bin/python pagefit.py --grant
```

Click through the dialog once. Afterwards measurement is silent.

No Word? Everything still runs; the page count becomes an estimate that can be
wrong by a line at the boundary, and `doctor` says so. LibreOffice is not a
substitute: with no Calibri it renders a correct one-page file as two pages.

---

## 10. First run

Use a saved posting rather than a URL for the first run — scraping is the least
reliable part, and a file removes it as a variable.

```bash
# paste the posting text into posting.txt, then:
.venv/bin/python run.py tailor \
  --jd-file posting.txt \
  --company "Acme" \
  --role "Product Manager Intern"
```

You should see the eligibility verdict with its quote, what the posting asks
for, the fit judgment, the edit plan, which proposed edits were rejected and
why, the recruiter score, and the measured page count.

Results go to `output/Acme/`: the .docx, the extracted posting profile, and a
report of what changed.

Then read the draft against your original, and check:

- does every number still match reality?
- do the swapped bullets say something the posting actually asked about?
- does it still sound like you?

When a rejection message looks wrong, that is worth an issue — the validators
are the product, and a false rejection is a real bug.

### Scraping from a URL

```bash
.venv/bin/python run.py tailor --url "https://..." --company "Acme" --role "..."
```

Many job boards render through JavaScript and give you boilerplate instead of
the posting. If the extracted text looks wrong, save the page text to a file and
use `--jd-file`. There is no fixing this in general.

---

## 11. Optional: approve postings from your phone

Only needed if something hands postings to this tool automatically. Skip it if
you run `tailor` yourself.

Without it, `tailor --auto` still works: it judges the posting on the light
backend, queues it, and spends nothing more. Approve from the terminal instead:

```bash
.venv/bin/python run.py pending                 # what is queued
.venv/bin/python run.py pending --make <id>     # build it (same as pressing ✅)
.venv/bin/python run.py pending --skip <id>     # pass on it
```

1. Create a bot with [@BotFather](https://t.me/botfather), note the token.
2. Message your bot once, then read your chat id from
   `https://api.telegram.org/bot<TOKEN>/getUpdates`.
3. Export both:

   ```bash
   export TG_BOT_TOKEN=123456:ABC...
   export TG_CHAT_ID=987654321
   ```

   With these unset, every notification falls back to console output and
   nothing breaks.

4. For the approve/skip buttons to work, the listener has to be running:

   ```bash
   .venv/bin/python bot_listener.py
   ```

   To keep it running across reboots on macOS, see
   `com.local.confirm-bot-listener.plist.example` — it has the install steps in
   its header. The real .plist is gitignored because it holds your token.

See [docs/operations.md](docs/operations.md) for the unattended loop, the
digest schedule, and what to do when the listener goes quiet.

---

## Troubleshooting

| symptom | cause | fix |
|---|---|---|
| `profile.yaml not found` | step 3 not done | `cp profile.example.yaml profile.yaml` and fill it in |
| `No LLM backend is available` | no CLI, no key, no local model | step 2; `doctor` lists every option |
| `FileNotFoundError` on a .docx | `resume_path` / `base_resume_path` wrong or moved | fix `user_config.yaml`, then `doctor` |
| minimal mode refuses to run | `base_resume.yaml` missing | re-run `bootstrap`, which writes it from your document |
| `base_resume.yaml does not match your résumé` | a heading differs by a character, an id does not exist, or a section is missing from the file | `doctor` lists each one with the fix; the run stops before spending anything |
| `the rendered document does not match what was selected` | your template's section headings or bullet formatting changed under the database | `doctor`, then check the headings in the .docx |
| every proposed edit is rejected | the model is proposing rewrites, not edits | expected, and the point. The report says which rule each one hit; your original ships |
| zero swaps, and the report says so | nothing in the database matched this posting better | add the missing experience to `resume_db.yaml` |
| page count "could not be measured" | no Word, or the access dialog is waiting | step 9, or accept the estimate |
| rate-limit message on `claude-cli` | the subscription window is shared with your interactive sessions | wait for the reset it names, or switch backend for that run |
| the extracted posting is boilerplate | JavaScript-rendered job board | save the text and use `--jd-file` |
| Telegram buttons do nothing | `bot_listener.py` is not running | start it (step 11), or approve from the terminal with `run.py pending --make <id>` |
| `--auto` produced no draft | that is the approval gate, not a failure | `run.py pending` lists it; `--make` builds it |
| `profile.yaml still has profile.example.yaml's values` | step 3 half-done | edit the fields the message names |
| `found no experience blocks` | the résumé is prose, or has no `EXPERIENCE` heading | step 4; compare against `templates/resume_template.docx` |

Still stuck: `doctor` first, then `doctor --probe`, then the report file in
`output/<Company>/`, which records every rejection with its reason.
