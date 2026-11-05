# Architecture

Where things are, what talks to what, and the invariants you should not break.

## The one idea

**Your approved résumé is the source of truth, and code — not a prompt — decides
whether an edit is allowed to replace it.**

An earlier version did the obvious thing: for each posting, pick bullets from a
database and have a model write them all. It produced sentences that were
technically accurate, under the character budget, and unreadable. The pressure
is structural: compressing facts into a fixed budget rewards telegraphic
notation, and a second "polish" pass compresses what the first one wrote.

So the default mode (`minimal`) does not author sentences. It starts from the
file you already send to employers and is allowed only to:

1. swap words for the posting's own vocabulary, and
2. replace one or two bullets with closer experience from your database.

Everything else in this repo exists to enforce those two limits, or to avoid
spending model calls on postings you would not apply to.

## Flow

```
  posting URL / text file
          │
          ▼
  ┌───────────────────────────────────────────────────────────┐
  │ jd_extractor.py                                           │
  │   fetch_jd_text_from_url      readability + HTML fallback │ code
  │   detect_sponsorship_exclusion regex, deliberately        │ code
  │   assess_eligibility           quotes the posting         │ LIGHT
  │   extract_jd_profile           what it asks for           │ LIGHT
  └───────────────────────────────────────────────────────────┘
          │        keyword_extractor.extract_keywords (frequency)   code
          ▼
  ┌───────────────────────────────────────────────────────────┐
  │ fit_gate.assess_prefit                                    │ LIGHT
  │   + profile.yaml target_roles  → floor/ceiling clamp      │ code
  │   + decisions.jsonl            → few-shot of your taste   │ code
  └───────────────────────────────────────────────────────────┘
          │
          ├── unattended? ─► confirm_state / digest / bot_listener ─► you press a button
          │                                                            (optional loop)
          ▼
  ┌───────────────────────────────────────────────────────────┐
  │ company_research.research_company   red flags, vocabulary │ HEAVY
  │ minimal_tailor.run                  plan the edit         │ HEAVY
  │   └─ apply_plan / check_text        validate the plan     │ code  ← most edits die here
  │ recruiter_review.run                score it, object      │ HEAVY
  │ resume_writer.render_sections       write the .docx       │ code
  │ pagefit / validate                  measure, revert       │ code
  └───────────────────────────────────────────────────────────┘
          │
          ▼
  output/<Company>/  —  .docx, jd_profile.json, report
```

`LIGHT` and `HEAVY` are the two backend roles in `llm_client.py`. Steps 1–7 are
cheap enough to run on a free local model; 8–11 are where model quality shows up
in what you send.

## Modules

### Entry points

| file | role |
|---|---|
| `run.py` | the CLI: `tailor`, `bootstrap`, `polish`, `review`, `digest`, `pending`, `doctor`, `decisions`. Orchestration lives here and only here. |
| `bot_listener.py` | long-polls Telegram for approve/skip presses. Optional. |
| `pagefit.py` | also a CLI (`pagefit.py --grant`, `pagefit.py file.docx`) for the Word measurement. |
| `validate.py` | also a CLI, for checking a generated .docx against the template. |

### Who you are

| file | role |
|---|---|
| `profile.py` | loads `profile.yaml` and turns it into prompt fragments (`applicant_block`, `eligibility_block`, `style_blob`, `applicant_phrase`) and role-matching patterns. **The only place the pipeline may state a fact about you** — including how the rewrite, review and polish prompts name you, which used to be the literal string "an MBA internship applicant". |
| `config.py` | paths, modes, thresholds. Personal values come from `user_config.yaml`; nothing personal is hardcoded, and nothing has a personal default. |

### Understanding a posting

| file | role |
|---|---|
| `jd_extractor.py` | fetch, eligibility screen, posting profile, sponsorship-exclusion regex |
| `keyword_extractor.py` | frequency-based terms for ATS matching |
| `company_research.py` | red flags and the company's own language |
| `fit_gate.py` | the pre-generation fit judgment, plus the clamps that keep a small model honest |
| `decisions.py` | the journal of what you approved, skipped, or let expire — the only real data about your taste |

### Editing the résumé

| file | role |
|---|---|
| `minimal_tailor.py` | **the default mode.** Plans and validates word-level edits and one-or-two bullet swaps. Every validator lives here. |
| `tailor_engine.py` | the older `rebuild` mode: select bullets from the database and rewrite them all. Kept for comparison; also holds the shared rewrite prompts and `reference_style_blob()`. |
| `recruiter_review.py` | scores a draft as a recruiter would, lists objections, and (only if you enable it) rewrites from them |
| `cover_letter.py` | cover-letter draft from the same material |

### Documents

| file | role |
|---|---|
| `templates/make_template.py` | writes `templates/resume_template.docx`, a résumé in the layout the parser and the renderer expect. Edit the script, not the .docx. |
| `resume_parser.py` | reads a .docx into sections and bullets (bootstrap only) |
| `bootstrap_engine.py` | turns that into a first `resume_db.yaml` |
| `resume_db.py` | loads and indexes the database; builds the selection blob |
| `resume_writer.py` | writes bullets back into the .docx, preserving its formatting |
| `pagefit.py` | drives Word via AppleScript to measure real pages and lines; finds widow lines |
| `validate.py` | structural checks on a generated .docx against the template |

### Plumbing

| file | role |
|---|---|
| `llm_client.py` | backend registry and role resolution (`light` / `heavy` / `auto`), plus `parse_json_loose` |
| `llm_utils.py` | pulls the text out of an API response, and normalises the look-alike characters a model emits (non-breaking hyphens and friends) before anything validates them |
| `vendor/ollama_client.py` | local model. Imported only when the `ollama` backend is chosen. |
| `vendor/claude_cli.py` | `claude -p`, with retries, rate-limit detection and a usage log |
| `vendor/command_cli.py` | any agent CLI, argv from config |
| `vendor/telegram.py` | the four notification levels; a no-op when unconfigured |
| `notify.py` | per-posting `Report` object: one message per posting, assembled across steps |
| `digest.py` | batches approval requests into one message at fixed hours |
| `confirm_state.py` | pending approvals, atomic writes, TTL, deduplication |
| `confirm_handler.py` | what a button press does |
| `genlock.py` | one generation at a time |
| `log_stamp.py` | timestamps every log line |
| `ask.py` | every interactive prompt. With no terminal it returns the default instead of raising EOFError, so a piped or scheduled run behaves like `--auto` |
| `errors.py` | `SetupError`: a problem the user has to fix. `run.py` prints it as a sentence and exits 1; everything else keeps its traceback |

### `vendor/`

Code this project needs but does not own. It used to be loaded from outside the
repository by absolute path, which meant a fresh clone could not run
`run.py --help`. Everything in `vendor/` is self-contained: no imports from
outside the repo, no shared state.

## Data

Four gitignored files, each with a committed `.example` twin whose comments are
the documentation.

| file | shape | who writes it |
|---|---|---|
| `profile.yaml` | degree, graduation, work authorisation, target roles, style examples | you |
| `user_config.yaml` | paths, mode, per-section caps, backends | `bootstrap`, then you |
| `resume_db.yaml` | every bullet you could use, with `context` (background **and** the allowed numbers) | `bootstrap`, then hours of you |
| `base_resume.yaml` | which bullets the approved résumé shows, keyed to .docx headings | `bootstrap` writes it from the document; you review it |

And runtime state, also gitignored:

| path | contents |
|---|---|
| `output/<Company>/` | drafts, extracted posting profile, reports, recruiter audit |
| `state/pending_confirmations.json` | postings awaiting a button press (each holds the posting text) |
| `state/decisions.jsonl` | the decision journal |
| `state/generation.lock` | the single-run lock |
| `state/cli_usage_log.json` | every CLI-backend call, for seeing how much you are spending |

### Why `context` is the important field

`context` is both the background a rewrite draws on and **the whitelist of
numbers**. `minimal_tailor._allowed_numbers()` collects the figures in the
bullet's `text` and `context`; anything else that appears in a rewritten
sentence is fabrication, and the edit is discarded. This is the mechanism behind
"it will not invent a number", and it is why the setup guide tells you to write
down figures today's sentence does not use.

## Invariants

Break these and the tool stops being trustworthy.

1. **Failure keeps the approved sentence.** Every validator's failure path ends
   in "ship what the human already approved". No gate may fall back to the
   model's version.
2. **Numbers come from the database, never from the model.** See above.
3. **`retired: true` is absolute.** A retired bullet cannot reach a résumé by
   any path — not selection, not swapping, not polish. It is enforced in the
   candidate list, in the apply step, and by a test.
4. **One page is measured, not estimated**, wherever Word is available; and
   spillover is fixed by reverting, never by compressing. Reverting converges
   because the original fits.
5. **Nothing is submitted.** The closest the code comes to applying is sending a
   .docx to your own Telegram chat. Keep it that way.
6. **Content is never lost silently.** After rendering, the document must hold
   exactly the bullets that were selected (`_verify_rendered` for the sentences,
   `_verify_render_counts` for the counts). In minimal mode a section the result
   does not cover is a `SetupError`, never a substituted database bullet — that
   substitution once produced a résumé with half its content while every check
   printed PASS.
7. **A broken setup costs nothing.** `_validate_inputs` runs before the first
   model call and validates base_resume.yaml against the database and the
   document, because these failures used to appear after the paid work.
8. **No personal fact in code.** Anything about the applicant belongs in
   `profile.yaml`. A prompt that needs a fact asks `profile.py` for it.
9. **No credential in a file.** `ANTHROPIC_API_KEY`, `TG_BOT_TOKEN`,
   `TG_CHAT_ID` come from the environment.
10. **Nothing requires a specific vendor.** Any backend that can answer a prompt
   can drive the pipeline; a missing Ollama is not an error.

## Changing things

**A new validator** → `minimal_tailor.py`, next to `check_text` / `check_swap`,
with a test that pins the sentence it rejects. Return a reason string: it is
shown in the report and is how a user tells a true rejection from a bug.

**A new LLM backend** → a class in `llm_client.py` with `.name`, `.complete()`
and a static `available()`, then add it to `_AUTO_ORDER`. If it is just another
CLI, you do not need code at all: add it under `backends:` in
`user_config.yaml`.

**A different résumé layout** → `resume_parser.py` (reading) and
`resume_writer.find_section_block` (writing). This is the least portable part of
the project; it assumes an `EXPERIENCE` heading, `Company, City` header lines and
bulleted achievements. `templates/make_template.py` builds a document in exactly
that shape, so it doubles as the executable specification of the layout — change
one and check the other.

**A new prompt** → keep it in the module that owns the step, as a module-level
template with `{named}` placeholders, and pass personal facts in from
`profile.py` rather than writing them into the text.

**Something about the applicant** → `profile.yaml` and `profile.example.yaml`
(with a comment explaining what breaks when it is wrong), then `profile.py`.
Never a literal in a prompt.
