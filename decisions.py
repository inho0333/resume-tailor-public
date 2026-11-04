"""The decision journal: a permanent record of the user's ✅ / ⏭️ / no-answer choices.

## Why (from a log analysis)

Over three weeks, 87 confirmation requests produced ✅ 8 / ⏭️ 5 / 43 expired after
seven days / 31 still waiting. That is **56 pieces of labelled preference data** that
already existed, and all of it survived only as a line in `logs/bot_listener.log`
while the `confirm_state` record was deleted at the moment of the decision. The only
real data available for the few-shot examples of the upstream filter (an upstream job
watcher, not part of this repo) and for calibrating the pre-fit assessment
(fit_gate.assess_prefit) was being thrown away.

An expiry is recorded as an "implicit no" — the user saw it for seven days and never
pressed — and in fact the 43 expiries leaned heavily toward Supply Chain / Logistics /
Scheduling roles, while all 8 approvals were Product Manager / Corporate Strategy
roles. That lean is exactly the signal the filter should be learning.

## Format

`state/decisions.jsonl` — one JSON object per line (append-only; if it is ever
corrupted, only that line is lost):
  {"ts", "decision": make|skip|expire, "company", "role", "url", "track",
   "eligible": yes|no|unclear, "visa_caution", "sponsorship_excluded",
   "prefit": high|mid|low|null,
   "prefit_reason", "must_have": [...], "source": button|prune|backfill}

The upstream job watcher's filter (not part of this repo) reads the same file
**read-only** — the file path is the contract between the two repos, so changing it
means changing both sides.
"""

import datetime
import json
import os

import config

PATH = os.path.join(config.STATE_DIR, "decisions.jsonl")

VALID_DECISIONS = ("make", "skip", "expire")


def record(decision, record_or_fields, source="button"):
    """Book one decision. `record_or_fields` is a confirm_state pending record (pass it
    straight through).

    A failed write must never block button handling, so every exception is swallowed —
    the journal is a secondary feature."""
    if decision not in VALID_DECISIONS:
        raise ValueError(f"Unknown decision: {decision!r}")
    r = record_or_fields or {}
    jd_profile = r.get("jd_profile") or {}
    eligibility = r.get("eligibility") or {}
    prefit = r.get("prefit") or {}
    entry = {
        "ts": datetime.datetime.now().isoformat(timespec="seconds"),
        "decision": decision,
        "company": r.get("company") or "?",
        "role": r.get("role") or "?",
        "url": r.get("url"),
        "track": jd_profile.get("track"),
        "eligible": eligibility.get("degree_eligible", "unclear"),
        "visa_caution": bool(eligibility.get("visa_caution")),
        "sponsorship_excluded": bool(eligibility.get("sponsorship_excluded")),
        "prefit": prefit.get("tier"),
        "prefit_reason": (prefit.get("reason") or "")[:200],
        "must_have": [m for m in (jd_profile.get("must_have_skills") or []) if m][:4],
        "source": source,
    }
    try:
        os.makedirs(config.STATE_DIR, exist_ok=True)
        with open(PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        os.chmod(PATH, 0o600)
    except OSError as e:
        print(f"  [warn] decision journal write failed (skipping): {e}")
    return entry


def load(limit=None):
    """Newest first. Broken lines are skipped quietly (the last line of an append-only
    file can be cut short)."""
    if not os.path.exists(PATH):
        return []
    out = []
    try:
        with open(PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    out.reverse()
    return out[:limit] if limit else out


def summary():
    """The distribution of decisions — the summary `run.py decisions` prints."""
    rows = load()
    counts = {d: 0 for d in VALID_DECISIONS}
    for r in rows:
        if r.get("decision") in counts:
            counts[r["decision"]] += 1
    return {"total": len(rows), "counts": counts, "rows": rows}


def fewshot_lines(max_positive=8, max_negative=8):
    """Few-shot lines to attach to the upstream filter / pre-fit assessment prompt.
    Newest first, with positives and negatives each capped.

    One line each, as `company — role → true/false`. The whole file is never dumped
    into a prompt (it is 56 entries already and keeps growing — prompt bloat is a
    standing problem in this system)."""
    pos, neg = [], []
    for r in load():
        line = f"{r.get('company', '?')} — {r.get('role', '?')}"
        if r.get("decision") == "make" and len(pos) < max_positive:
            pos.append(f"{line} → true (the user actually made a draft)")
        elif r.get("decision") in ("skip", "expire") and len(neg) < max_negative:
            why = ("skipped it explicitly" if r["decision"] == "skip"
                   else "saw it for seven days and never pressed")
            neg.append(f"{line} → false (the user {why})")
        if len(pos) >= max_positive and len(neg) >= max_negative:
            break
    return pos + neg
