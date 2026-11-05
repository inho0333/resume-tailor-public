# Operations

Running it day to day, and running it unattended.

## Commands

```bash
# tailor for one posting (interactive)
.venv/bin/python run.py tailor --url "https://..." --company "Acme" --role "Summer Associate"

# from saved text — more reliable than scraping, and the right default for a first run
.venv/bin/python run.py tailor --jd-file posting.txt --company "Acme" --role "Summer Associate"

# a different template (its headings must match base_resume.yaml / resume_db.yaml)
.venv/bin/python run.py tailor --jd-file posting.txt --resume /path/to/other_template.docx

# score an existing draft as a recruiter would; --apply also rewrites from the objections
.venv/bin/python run.py review --company "Acme" --role "Product Manager Intern"
.venv/bin/python run.py review --company "Acme" --role "Product Manager Intern" --apply

# rewrite an existing draft with the heavy backend (fully manual)
.venv/bin/python run.py polish --company "Acme" --role "Product Manager Intern"

# send the queued approval requests now, instead of at the next digest hour
.venv/bin/python run.py digest

# what is waiting for approval, and build or skip one without Telegram
.venv/bin/python run.py pending
.venv/bin/python run.py pending --make 5d96906653

# what you have approved, skipped, or let expire
.venv/bin/python run.py decisions --limit 20

# measure a file's real page count, change nothing
.venv/bin/python pagefit.py "output/Acme/Resume_Acme_PMIntern_20260824.docx" --json

# re-run the structural validators on a saved file
.venv/bin/python validate.py "output/Acme/Resume_Acme_PMIntern_20260824.docx"

# check the whole setup; --probe also sends one real prompt per role
.venv/bin/python run.py doctor --probe
```

Interactive `tailor` stops for you at most twice: once to ask about gaps the
posting wants and your database cannot evidence (Enter skips), and once to show
the planned edit before writing the file (`n` cancels, writing nothing).

`--auto` is a different shape, not just a quieter one: it runs the light-backend
steps (eligibility, posting profile, keywords, fit), **queues the posting, and
stops**. No research, no rewrite, no .docx — those cost minutes and model quota,
and they only start when you approve:

```bash
.venv/bin/python run.py pending              # what is queued, with the fit tier
.venv/bin/python run.py pending --make <id>  # build it (same code path as ✅)
.venv/bin/python run.py pending --skip <id>  # pass on it (recorded in the journal)
```

Pressing ✅ in Telegram does exactly what `--make` does. Telegram is the
convenience, not the mechanism.

## What a run produces

In `output/<Company>/`:

| file | contents |
|---|---|
| `Resume_<Company>_<Role>_<YYYYMMDD>.docx` | the draft |
| `<Company>_<Role>_jd_profile.json` | the extracted posting profile, plus company research under `company_research` — reusable for a cover letter |
| `<Company>_<Role>_jd_text.txt` | the posting text (`polish` re-reads it to recount keywords) |
| recruiter audit | the score and every objection, when the review ran |

`validate.py` runs automatically right after the build and reports double
dashes, mixed fonts, over-long bullets and page overflow; the page verdict from
`pagefit.py` also goes into the notification. Overflow is judged by measuring
your template with the same estimator, not against an absolute number.

Everything under `output/` and `state/` is gitignored.

## One generation at a time

`genlock.py` serialises generation: `state/generation.lock`, 20-minute timeout.
If the unattended loop is mid-draft, a manual `review --apply` waits for it
rather than writing the same file from two processes.

## Company research

Every `tailor` run pulls recent headlines for the company from Google News RSS
(no API key) and asks the heavy backend to summarise only what would change how
you apply — red flags, reorganisations, hiring signals — or to say there is
nothing notable.

It is **not** injected into the bullet-rewrite prompt. It prints to the console,
is saved in `jd_profile.json`, and is sent to you when notable. A failure here
never stops the run: it is context for a human, not an input to the résumé.

## Running unattended

Only needed if something hands postings to this tool automatically. Three parts.

### 1. The listener

`bot_listener.py` polls Telegram in a loop and does five things: handles button
callbacks, expires stale requests, sends the approval digest, writes a
heartbeat, and rotates its log.

```bash
.venv/bin/python bot_listener.py          # foreground, for trying it out
```

To keep it alive across reboots on macOS, copy
`com.local.confirm-bot-listener.plist.example` to
`com.local.confirm-bot-listener.plist`, fill in the repo path and your token,
and load it (the file's header has the exact steps). Then:

```bash
launchctl list | grep confirm-bot-listener   # a PID means it is alive
tail -20 logs/bot_listener.log
cat state/listener_heartbeat.json            # {"ts","pid","pending"}, refreshed every minute
launchctl kickstart -k gui/$UID/com.local.confirm-bot-listener   # restart
```

**A running daemon holds the code it started with.** Edit a module and the
daemon keeps running the old version until it restarts. The listener checks
`.py` modification times once a minute and exits on a change so launchd brings
it back on the new code (deferring if a generation is in progress), but if you
want a change live now, `kickstart`.

This matters more than it sounds. A daemon that had been up for ten days never
once executed a one-page guarantee added on day nine — the feature was
committed, tested, and simply not running.

### 2. The heartbeat

`state/listener_heartbeat.json` is written every minute. Whatever supervises
this (an upstream job watcher, a cron check, a second machine) should alert when
it stops moving for 15 minutes.

Watch it from **a different Python environment** than this repo's `.venv`. The
failure this guards against is the venv itself breaking: a dependency vanished,
the listener failed eleven polls in a row, and nothing said so because the thing
that would have complained was also broken.

### 3. The approval loop

```
posting arrives → eligibility + fit judged (light backend, seconds)
                → confirm_state.register()  … the posting text is stored with it
                → queued
     at digest_hours → one message, one row per posting, buttons per row
     you press ✅ → confirm_handler → the expensive steps run → draft arrives
     you press ⏭️ → recorded in decisions.jsonl
     nobody presses → expires after confirm_ttl_days, also recorded
```

Three settings in `user_config.yaml` shape this:

- **`digest_hours: [9, 20]`** — batching, instead of one message per posting.
  Immediate sends produced fifteen messages in a day, which is how you stop
  reading them. An empty list restores immediate sends.
- **`confirm_ttl_days: 7`** — each pending record holds a whole posting, and a
  three-week-old posting has usually closed.
- **`prefit_ask_min_tier: mid`** — postings judged below this get a single
  "draft it anyway" button rather than an approve/skip pair. In the log that
  shaped this setting, most low-tier requests expired unanswered: they were roles
  that were never going to be applied to, and each one still cost a message.

Every button press and every expiry goes into `state/decisions.jsonl`, and
`fit_gate` feeds that history back into the next fit judgment as examples. The
longer you use it, the more the fit judgment matches your actual taste — so
pressing ⏭️ is worth more than ignoring a message.

## Watching what it costs

CLI-backend calls are logged to `state/cli_usage_log.json`:

```bash
.venv/bin/python -c "from vendor import claude_cli; print(claude_cli.usage_summary())"
```

Counts by tag, so you can see whether an unattended loop is calling more than
you thought.

With `claude-cli`, remember the rate limit is **shared with your own interactive
Claude Code sessions**. A window you exhausted by hand will fail these calls;
the error names the reset time, and the run stops cleanly rather than producing
a worse draft.

## Word access, once

The page measurement opens a copy at a fixed path
(`output/.pagefit/pagefit_probe.docx`), so macOS only ever asks about one file:

```bash
.venv/bin/python pagefit.py --grant      # click Grant Access when it appears
```

Until you do, measurement gives up after 75 seconds and falls back to the
estimate, and the report's length line says the measurement failed. An
unattended run cannot click a dialog, which is the whole reason this is a
separate one-time command.

## When something looks wrong

| symptom | first thing to check |
|---|---|
| nothing happens on a button press | is `bot_listener.py` running? `launchctl list \| grep confirm` — or skip the bot: `run.py pending --make <id>` |
| `--auto` finished and there is no draft | that is the approval gate. `run.py pending` lists it |
| a fix you made is not taking effect | the daemon is holding old code — `kickstart`, and check `heartbeat.ts` moved |
| drafts stopped arriving | `tail logs/bot_listener.log`; then `run.py doctor` |
| every edit is rejected | expected. The report names the rule each one hit; the baseline ships |
| page count unmeasured | `pagefit.py --grant`, or accept the estimate |
| rate-limit errors | `claude-cli` shares its window with interactive use; wait, or switch backend for the run |
| "import error" after a `git pull` | recreate the venv: `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`, then restart the listener |

`run.py doctor` is the fastest triage for all of them.
