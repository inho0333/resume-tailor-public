"""The cheap filter in front of everything else: is this posting even about you?

A source returns what matches its own filters, and those are coarse. An
aggregator's "operations" tag covers supply-chain scheduling and product
operations equally; measured on one batch of 50, between 15 and 20 were
genuinely related to the person searching. Putting all 50 through the full
pipeline is affordable in money (the first stages run on a local model) but not
in attention: a digest of 50 questions is a digest nobody answers.

So each posting gets one short judgment first, on the light backend, from three
inputs that already exist:

- `profile.yaml` — who is searching and what for
- `resume_db.yaml` bullet text — what they can actually evidence
- `decisions.jsonl` — what they have approved and skipped before, as few-shot
  examples. This is the only record of their taste, and it is the input that
  improves on its own.

## Fail open

Every failure here lets the posting through: an unreachable model, an
unparseable reply, a missing database. This is the opposite of a spam filter,
and deliberately so — a false positive costs one line in a digest, a false
negative costs a job you never heard about. The same rule governs the title and
company block lists below, which is why they start empty.

## Not a second fit judgment

`fit_gate` asks "how much of this posting can this career evidence" and produces
the tier you see. This asks only "is this the same universe of work", and its
answer never reaches the digest. Two filters with the same question would be one
filter run twice; the point of this one is to run before anything is queued.
"""

import os

import config
import llm_client
import profile as profile_module

PROMPT = """\
You are filtering job postings for one person. Answer only whether this posting is in \
the same kind of work as their experience — not whether they would get it.

[The person]
{applicant_block}

[What they have actually done]
{experience}
{history_block}
[The posting]
Company: {company}
Title: {title}
{body}

Answer with this JSON and nothing else:
{{"related": true or false, "why": "at most 12 words"}}

"related": true if the work is in the same family as their experience or a plausible \
next step from it. false only when it is plainly a different line of work.
"""


def _experience_blob(limit=40):
    """Bullet text from the experience database, nothing else.

    No `context`, no tags: this judgment needs to know what kind of work the
    person has done, and the full database blob runs to tens of kilobytes.
    """
    try:
        import resume_db

        sections = resume_db.load_db(config.DB_PATH)
    except Exception:
        # No database yet — on a fresh setup this filter simply does not run.
        return ""
    lines = []
    for section in sections:
        for bullet in section.get("bullets") or []:
            if bullet.get("retired") or bullet.get("interview_only"):
                continue
            lines.append(f"- {bullet.get('text', '')}")
            if len(lines) >= limit:
                return "\n".join(lines)
    return "\n".join(lines)


def _history_block():
    try:
        import decisions

        lines = decisions.fewshot_lines(max_positive=8, max_negative=8)
    except Exception:
        lines = []
    if not lines:
        return "\n"
    return ("\n[What they decided about earlier postings]\n"
            + "\n".join(f"- {line}" for line in lines) + "\n")


def _blocked(posting):
    """A title or company you never want to see again. Empty by default.

    This is the one fail-CLOSED step in the tracker, so it is yours to fill and
    starts empty. The version of this list in the private pipeline it came from
    had grown real employer names from months of use — which is exactly why it
    cannot ship with contents: one person's noise is another's target.

    Set tracker.block_titles and tracker.block_companies in user_config.yaml.
    Matching is a lowercase substring, and a match drops the posting silently.
    """
    title = (posting.title or "").lower()
    company = (posting.company or "").lower()
    for word in (config.TRACKER_BLOCK_TITLES or []):
        if word.lower() in title:
            return f"title contains {word!r}"
    for word in (config.TRACKER_BLOCK_COMPANIES or []):
        if word.lower() in company:
            return f"company matches {word!r}"
    return None


def judge(posting, backend=None, log=print):
    """Returns {"related": bool, "why": str, "blocked": str|None}.

    Anything that goes wrong returns related=True. See "Fail open" above.
    """
    blocked = _blocked(posting)
    if blocked:
        return {"related": False, "why": blocked, "blocked": blocked}

    experience = _experience_blob()
    if not experience:
        return {"related": True, "why": "no experience database to compare against",
                "blocked": None}

    body = (posting.text or "").strip()
    body = f"Posting:\n{body[:1500]}" if body else "(the listing carried no body text)"

    prompt = PROMPT.format(
        applicant_block=profile_module.get().applicant_block(),
        experience=experience,
        history_block=_history_block(),
        company=posting.company or "(unknown)",
        title=posting.title or "(unknown)",
        body=body,
    )
    try:
        backend = backend or llm_client.get_backend()
        parsed = llm_client.parse_json_loose(backend.complete(prompt)) or {}
        related = bool(parsed.get("related", True))
        why = str(parsed.get("why") or "").strip()[:80]
    except Exception as e:
        log(f"  [relevance check failed — letting it through] {e}")
        return {"related": True, "why": f"check failed: {e}", "blocked": None}
    return {"related": related, "why": why, "blocked": None}


def enabled():
    """Off when there is no experience database to compare against yet."""
    return bool(config.TRACKER_RELEVANCE_FILTER and os.path.exists(config.DB_PATH))
