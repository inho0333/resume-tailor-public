"""What has already been seen, so a posting is only ever asked about once.

Two layers, because one is not enough:

1. **By source and id.** The posting's own identifier in the source that found
   it. Catches the ordinary case: the same board polled again five minutes later.

2. **By company and title, across sources.** The same opening reached through
   two sources has two unrelated ids, and nothing in layer 1 can tell. Measured
   twice: one posting arrived as a company's own Greenhouse id and again as an
   aggregator's id, and ran the whole drafting pipeline twice; another was
   duplicated inside a single aggregator, once as a direct link and once as a
   reposting from a job-board site.

Both layers are deliberately leaky in the same direction. The cross-source
window expires (`CROSS_SOURCE_DAYS`) so the same title reopening next season is
treated as new, and anything unparseable is treated as unseen. Missing a real
opening is worse than one duplicate notification.

## Two orderings that matter

**A posting the relevance filter drops is not marked seen** — it goes into its
own `_filtered` bucket instead. Marking it seen is what the pipeline this came
from did, and it burned those postings permanently: improving the filter changed
nothing for anything it had already rejected, because the state said they were
handled. A separate bucket is still cheap to skip (no model call on later ticks)
and `run.py watch --recheck-filtered` can reconsider the lot.

**A posting that survives is marked seen before it is queued**, not after.
Judging and queueing takes seconds to minutes, and a crash in the middle would
otherwise re-notify everything on the next tick.
"""

import datetime
import json
import os
import re
import tempfile

import config

PATH = os.path.join(config.STATE_DIR, "seen_postings.json")

# The bucket name cannot collide with a source name. A leading underscore is not
# a legal source name, which is what keeps them apart.
_FINGERPRINT_BUCKET = "_fingerprints"

CROSS_SOURCE_DAYS = 45
PRUNE_AFTER_DAYS = 180

# Company names differ between sources, usually by a legal suffix.
_LEGAL_SUFFIXES = {
    "corporation", "corp", "inc", "incorporated", "llc", "ltd", "limited",
    "co", "company", "gmbh", "plc", "ag", "sa", "pte", "kk", "holdings", "group",
}


def _norm(value):
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()


def _norm_company(company):
    return " ".join(t for t in _norm(company).split() if t not in _LEGAL_SUFFIXES)


def load(path=PATH):
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError) as e:
        # A corrupted state file must not stop the watch. The cost of starting
        # from empty is one round of duplicate notifications, which is
        # recoverable; refusing to run is not.
        print(f"  [warn] dedupe state unreadable ({e}) — starting from empty")
        return {}


def save(state, path=PATH):
    """Atomic: written to a temp file in the same directory, then renamed.

    A half-written state file would read as "nothing has ever been seen", and
    the next tick would re-notify every posting in every source.
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    except OSError:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


# ── layer 1: by source and id ───────────────────────────────────────────────

def is_new(state, source, posting_id):
    return str(posting_id) not in (state.get(source) or {})


def mark_seen(state, posting):
    state.setdefault(posting.source, {})[posting.id] = {
        "first_seen": datetime.datetime.now().isoformat(timespec="seconds"),
        "title": posting.title,
        "url": posting.url,
    }
    return state


def count(state, source):
    return len(state.get(source) or {})


# ── layer 2: by company and title, across sources ──────────────────────────

def _variant(bucket, company, title):
    """The same posting under a different spelling of the company name.

    Only when the title matches **exactly** and one company name is a prefix of
    the other — "Rocket Lab" and "Rocket Lab Corporation" are one employer, but
    a title as generic as "Product Management Intern" must not collapse across
    two genuinely different companies. Erring here costs a missed opening, which
    is the expensive mistake.
    """
    want_title = _norm(title)
    ours = _norm_company(company)
    if not ours:
        return None
    for key, record in bucket.items():
        prev_company, _, prev_title = key.partition("|")
        if prev_title != want_title:
            continue
        theirs = _norm_company(prev_company)
        if theirs and (ours.startswith(theirs) or theirs.startswith(ours)):
            return record
    return None


def is_duplicate(state, posting, within_days=CROSS_SOURCE_DAYS):
    bucket = state.get(_FINGERPRINT_BUCKET) or {}
    record = bucket.get(posting.fingerprint()) or _variant(
        bucket, posting.company, posting.title)
    if not record:
        return False
    try:
        seen_at = datetime.datetime.fromisoformat(record["first_seen"])
    except (KeyError, ValueError):
        return False
    return datetime.datetime.now() - seen_at <= datetime.timedelta(days=within_days)


def mark_fingerprint(state, posting):
    state.setdefault(_FINGERPRINT_BUCKET, {})[posting.fingerprint()] = {
        "first_seen": datetime.datetime.now().isoformat(timespec="seconds"),
        "title": posting.title,
        "url": posting.url,
    }
    return state


def mark(state, posting):
    """Both layers at once. The only marking call `watch` makes."""
    mark_seen(state, posting)
    mark_fingerprint(state, posting)
    return state


# ── dropped by the relevance filter ─────────────────────────────────────────
#
# A posting the filter rejected is kept apart from the ones marked seen, which
# matters more than it sounds. In the pipeline this came from, a rejected posting
# was marked seen — so improving the filter changed nothing for anything it had
# already dropped, permanently, and there was no way to tell the difference
# between "you were asked about this" and "something decided not to ask you".
#
# Kept in its own bucket, it is cheap to skip on later ticks (no model call) and
# `run.py watch --recheck-filtered` can reconsider the lot.

_FILTERED_BUCKET = "_filtered"


def was_filtered(state, posting):
    return posting.fingerprint() in (state.get(_FILTERED_BUCKET) or {})


def mark_filtered(state, posting, why=""):
    state.setdefault(_FILTERED_BUCKET, {})[posting.fingerprint()] = {
        "first_seen": datetime.datetime.now().isoformat(timespec="seconds"),
        "title": posting.title,
        "url": posting.url,
        "why": why,
    }
    return state


def clear_filtered(state):
    """Forget every filter rejection, so the next tick judges them again."""
    state.pop(_FILTERED_BUCKET, None)
    return state


# ── first run ───────────────────────────────────────────────────────────────

def baseline(state, postings):
    """Mark everything a source can currently see, and notify about none of it.

    The first poll of a source sees its whole current index. Without this, the
    first run of a new source sends one notification per posting — measured at
    around fifty on an aggregator, which is both useless and the kind of thing
    that gets a tool uninstalled on day one. From the next poll on, only
    genuinely new postings arrive.
    """
    for posting in postings:
        mark(state, posting)
    return state


def prune(state, days=PRUNE_AFTER_DAYS):
    """Drop entries older than `days`. An unparseable timestamp is kept.

    Keeping what cannot be read is the safe direction: dropping it would make
    that posting new again and re-notify it.
    """
    cutoff = datetime.datetime.now() - datetime.timedelta(days=days)
    pruned = {}
    for bucket, entries in state.items():
        kept = {}
        for key, info in (entries or {}).items():
            try:
                seen_at = datetime.datetime.fromisoformat(info["first_seen"])
            except (KeyError, TypeError, ValueError):
                kept[key] = info
                continue
            if seen_at >= cutoff:
                kept[key] = info
        if kept:
            pruned[bucket] = kept
    return pruned
