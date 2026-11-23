# The tracker

Stage 1: finding the postings. `tracker/CHANNELS.md` is the user-facing list of
sources — what each covers, what it costs to set up, where it falls short. This
file is how the thing works and how to add to it.

## The shape

```
sources/*.py  →  Posting  →  dedupe  →  jd_lookup  →  relevance  →  confirm_state
                             (seen      (fill in      (is this        (the approval
                              it?)       the body)     my field?)      queue)
```

A source does one thing: return the postings it can see **right now**. Not "what
is new" — deduplication is shared, so a source keeps no state and can be tested
by calling `fetch()` twice.

After the queue, a posting is on exactly the path a `tailor --auto` run takes:
`confirm_state.register` → `digest` → your ✅ → `generate_from_pending`. That is
deliberate. The tracker is an alternative way of arriving, not a second
pipeline, so a fix to the fit gate or the edit validators applies to both.

### The handoff is an in-process call

`cmd_watch` builds a `cmd_tailor` argument set and calls it. The pipeline this
grew out of did the same thing as a subprocess, in a second repository, with its
own virtualenv, handing the posting body over through a temp file — which added
a second dependency tree to keep working, a second place for a broken venv to
hide, and a watchdog whose whole job was noticing that the other repo's daemon
had died. None of that is needed to call a function.

## Adding a source

One file in `tracker/sources/`, one line in `tracker/__init__.py`, one block of
documentation in `tracker/CHANNELS.md`. `tracker/sources/remotive.py` is the
shortest complete example.

```python
from tracker import posting as posting_mod
from tracker.sources import base


class Source(base.Source):
    name = "example"
    summary = "One sentence for `run.py setup`: what it covers, what it costs."

    def check_auth(self):
        data = self._get_json("https://example.com/api/jobs")
        if data is None:
            return base.Status(False, "example.com did not answer",
                               fix="check EXAMPLE_API_KEY")
        return base.Status(True, f"{len(data)} postings visible")

    def fetch(self):
        return [posting_mod.Posting(
            id=str(job["id"]), source=self.name,
            company=job["employer"], title=job["title"], url=job["link"],
            updated_at=job.get("posted_at", ""), text=job.get("description", ""),
            facts={"location": job.get("city", "")},
        ) for job in (self._get_json("https://example.com/api/jobs") or [])]
```

Then:

```python
# tracker/__init__.py
SOURCE_NAMES = (..., "example")
_REGISTRY = {..., "example": example.Source}
```

`run.py setup`, `run.py doctor` and `run.py watch` pick it up with no further
changes. If it needs settings, add a branch to `setup_wizard._configure_source`
and document the keys in `user_config.example.yaml`.

### `check_auth` is not optional

A source that cannot run returns an empty list, and an empty list is
indistinguishable from a quiet day. That is not hypothetical: one board token
was a placeholder from the start, was polled every two minutes for a week,
answered 404 every time, and was reported throughout as "no new postings". So
`check_auth` makes one real request where it can, and `Status.fix` carries the
command that fixes it rather than advice about what ought to be done.

### What belongs in `facts`

Only what the posting **states**. The keys the fit gate screens on are
`location`, `work_arrangement`, `employment_type`, `comp_range`,
`years_required`; `_facts_for_pipeline` in `run.py` passes those through and
drops anything else, so a source cannot introduce a new axis by accident.

Do not infer. One board's API reports every posting as remote, including ones
whose location is the company's headquarters — mapping that onto
`work_arrangement` would mark everything remote, which switches off the location
check for all of them and tells someone searching remote-only that an on-site
role qualifies. An absent fact is screened on by nobody; a wrong one is screened
on by everybody.

Use `base.normalise_employment` for employment type: every source spells it
differently (`FullTime`, `Full-time`, `Intern`) and the fit gate compares it
against one vocabulary.

## Deduplication

Two layers, in `tracker/dedupe.py`.

**By source and id.** The ordinary case: the same board polled again.

**By company and title, across sources.** The same opening reached two ways has
two unrelated ids. Measured twice: one posting arrived as a company's own
Greenhouse id and again as an aggregator's, and ran the whole drafting pipeline
twice; another was duplicated inside one aggregator, as a direct link and as a
reposting. Company names are compared with legal suffixes stripped, and only
when the title matches exactly and one name is a prefix of the other — "Rocket
Lab" and "Rocket Lab Corporation" are one employer, but "Product Management
Intern" must not collapse across two unrelated companies.

The cross-source window expires after 45 days so the same title reopening next
season counts as new, and anything unparseable counts as unseen. Both leaks are
in the same direction on purpose: missing a real opening costs more than one
duplicate notification.

### Two orderings

A posting the relevance filter drops goes into a `_filtered` bucket, **not** the
seen bucket. The pipeline this came from marked it seen, which burned it:
improving the filter changed nothing for anything already rejected, and nothing
distinguished "you were asked about this" from "something decided not to ask
you". A separate bucket stays cheap to skip and
`run.py watch --recheck-filtered` reconsiders the lot.

A posting that survives is marked seen **before** it is queued. Judging and
queueing takes seconds to minutes, and a crash in the middle would otherwise
re-notify everything on the next tick.

## Filling in the posting body

`tracker/jd_lookup.py`, cheapest route first: the employer's own ATS API when
the URL names one, then the page's schema.org `JobPosting` block, then its
visible text. Each rung is tried only when the one above produced less than 200
characters — the same threshold the drafting pipeline needs, because "extraction
succeeded" should not be true of a result nothing can be judged on. Measured on
JavaScript-rendered pages: naive scraping often returns the `<title>` tag and
nothing else, at 114, 153 and 157 characters, which is enough to look like
success.

There is no headless-browser rung. It would catch the single-page applications
the static rungs cannot read, and it costs a Playwright install, a browser
download and 4–8 seconds per posting. A posting whose body cannot be read is
reported as such and still reaches you with its link; it arrives without a fit
judgment worth trusting.

One specific trap is checked for: an extractor that captures the application
**form's** field schema instead of the posting. Measured, one page produced
3,956 hits on tokens like `questionid` and `choices` where real postings produce
zero. Without the check, that JSON became the posting — its top ATS keywords
were "label" and "choices", and the degree screen reported "unclear" because the
requirements were never in the text it was given.

## The relevance filter

`tracker/relevance.py`. One short judgment per posting, on the light backend,
before anything is queued: is this the same kind of work at all? A source's own
filters are coarse — an aggregator's "operations" tag covers supply-chain
scheduling and product operations equally, and on one measured batch of 50
postings between 15 and 20 were genuinely related.

It reads three things that already exist: `profile.yaml`, the bullet text in
`resume_db.yaml`, and `state/decisions.jsonl` — what you have approved and
skipped before, as few-shot examples. That last input is the only one that
improves on its own.

**It fails open.** An unreachable model, an unparseable reply, a missing
database: every failure lets the posting through. This is the opposite of a spam
filter, and deliberately so — a false positive costs one line in a digest, a
false negative costs a job you never heard about.

The one fail-closed step is `tracker.block_titles` / `tracker.block_companies`
in `user_config.yaml`, and it ships empty. The version of that list in the
pipeline this came from had accumulated real employer names over months of use,
which is exactly why it cannot ship with contents: one person's noise is
another's target.

It is not a second fit judgment. `fit_gate` asks "how much of this posting can
this career evidence" and produces the tier you see; this asks only "is this the
same universe of work", and its answer never reaches the digest.

## Running it unattended

```bash
python run.py watch                      # one tick, then exit
python run.py watch --source greenhouse  # just one source
python run.py watch --recheck-filtered   # reconsider what the filter dropped
```

There is no loop and no daemon here: one invocation is one tick. Whatever
schedules it is the loop, which means a crash costs one tick rather than the
whole watch, and the code can be changed without restarting anything.

Schedule it the way your machine schedules things — launchd on macOS, cron or a
systemd timer on Linux. Every two minutes is what the private version of this
runs at; anything from a few minutes to hourly is reasonable, since the digest
batches the results anyway.

**The trap worth knowing before you schedule it:** a launchd job's `PATH` is
`/usr/bin:/bin:/usr/sbin:/sbin`, and Homebrew is not on it. A Node-based CLI
like Trackly's fails only under the scheduler, because both the binary and its
`#!/usr/bin/env node` shebang resolve through `PATH` — testing it by hand always
passes. `tracker/sources/trackly.py` passes an explicit `PATH` for exactly this
reason; any source that shells out should do the same.
