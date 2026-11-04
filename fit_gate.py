"""Is this posting worth applying to? Judged BEFORE you are asked to approve it.

## Why the judgment happens here

An earlier version judged fit *after* generation. That was useless twice over:
the verdict never reached the user (it was printed to a console nobody read),
and the expensive work had already run.

The place a fit judgment is actually worth something is the approval message.
Measured over three weeks: 87 approval requests, 13 answered, 43 expired
unanswered — and every approval clustered in a handful of role types. People
decide by *what the job is*, but the message only carried eligibility and visa
lines. So fit is judged first, cheaply, and shown in the message.

Generation has not happened yet, so there are no selected bullets to judge. The
question asked instead is "how much of what this posting requires can this
experience database actually evidence".

## Not trusting the model

A small local model, given the same posting three times, returned "low" once,
reasoning "this person does not have an MBA" — because the prompt had not told
it the degree. Hence the guards:
  - a title matching one of your `target_roles` cannot be pushed below "mid";
    missing a posting you wanted costs more than one noisy notification
  - a title matching `avoid_roles` is capped at "mid" however much the model
    likes it
  - "high" requires the model AND the title to agree
  - your real past approvals and skips (decisions.jsonl) go in as few-shot
    examples

All of it degrades to "ask anyway": a judgment that cannot be made must not
become a posting you never saw.
"""

import re

import config
import decisions
import llm_client
import profile as profile_module

TIERS = ("high", "mid", "low")
_TIER_ORDER = {t: i for i, t in enumerate(("low", "mid", "high"))}

TIER_LABEL = {
    "high": "strong fit",
    "mid": "possible fit",
    "low": "weak fit",
}


def _role_patterns():
    """(floor patterns, ceiling patterns) from profile.yaml.

    Both lists are statements about what *you* want, which no model can infer.
    Empty lists — the default on day one — mean no clamping: the model's
    judgment stands as-is.
    """
    prof = profile_module.get()
    return prof.target_role_patterns(), prof.avoid_role_patterns()


PREFIT_PROMPT_TEMPLATE = """\
You are screening internship postings for one applicant. Decide whether this \
posting is worth applying to.

[Applicant]
{applicant_block}

[Everything the applicant can evidence]
{db_blob}

[Posting]
Company: {company}
Role: {role}
Track: {track}
Required: {must_have}
Responsibilities: {responsibilities}
What they say they want: {ideal}

[Eligibility screen] {eligibility_line}
{history_block}
How to decide:
- "high": the core work of this posting directly overlaps the applicant's \
experience, and most of the stated requirements can actually be evidenced by it.
- "mid": there is real overlap, but some of the core work cannot be evidenced. \
Applying is still reasonable.
- "low": the experience above contains nothing for the core work of this \
posting, or the nature of the role is not what the applicant is looking for \
(shift work, field operations, pure administration, a degree restriction).

In `unmet`, list at most two things THIS POSTING REQUIRES that the experience \
above cannot evidence. Be specific and short.

Answer with this JSON and nothing else:
{{"tier": "high"|"mid"|"low", "reason": "one line on why", "unmet": ["...", "..."]}}
"""


def _compact_db_blob(db_sections):
    """Database summary for the fit judgment: `text` only, no context.

    The full blob with every bullet's context runs to tens of kilobytes, which is
    both slow and unnecessary here — fit only needs to know what experience
    exists. Context is what a *rewrite* needs.
    """
    lines = []
    for section in db_sections:
        lines.append(f"### {section['name']}")
        for b in section["bullets"]:
            if b.get("retired"):
                continue
            lines.append(f"- {b['text']}")
    return "\n".join(lines)


def _pattern_signal(role, company=""):
    """Deterministic signal from the title. Returns "strong" | "weak" | None."""
    strong, weak = _role_patterns()
    blob = f"{role or ''} {company or ''}".lower()
    for p in strong:
        if re.search(p, blob):
            return "strong"
    for p in weak:
        if re.search(p, blob):
            return "weak"
    return None


def _history_block():
    lines = decisions.fewshot_lines(max_positive=6, max_negative=6)
    if not lines:
        return "\n"
    return ("\n[What this applicant actually decided before] (postings they drafted or skipped "
            "— the evidence of their taste)\n"
            + "\n".join(f"- {l}" for l in lines) + "\n")


def _clamp(tier, signal):
    """Apply the floor and ceiling from the title (see the module docstring)."""
    if signal == "strong" and _TIER_ORDER[tier] < _TIER_ORDER["mid"]:
        return "mid", "title matches a role you do apply to"
    if signal == "weak" and _TIER_ORDER[tier] > _TIER_ORDER["mid"]:
        return "mid", "title matches a role you have never chosen"
    if tier == "high" and signal != "strong":
        # Small models hand out "high" freely. Measured on one batch of 24
        # pending postings: two operations-analyst internships both came back
        # "high", and every posting of that kind had expired unanswered. "high"
        # is only given when the model and the title agree, because the digest
        # sorts on it.
        return "mid", "no target-role match in the title, so high -> mid"
    return tier, None


def _degree_pattern():
    """Regex for your degree as postings write it, from profile.yaml.

    Used only to catch a self-contradicting eligibility verdict (below).
    """
    program = (profile_module.get().program or "").strip()
    if not program:
        return None
    return re.compile(r"\b" + r"\s+".join(re.escape(w) for w in program.split()) + r"\b", re.I)


def eligibility_contradicted(jd_profile, eligibility):
    """Reason to distrust an "ineligible" verdict, or None.

    Measured cases, both from a single batch: a posting titled "Product Manager
    **MBA Intern**" was judged ineligible because the screen quoted "BS or MS in
    Computer Science" from elsewhere in the page; a posting whose requirement
    read "pursuing an MS in Electrical Engineering **or MBA**" was judged
    ineligible as "not a technical degree". Those are the postings that matter
    most, and they were being dropped silently.
    """
    if (eligibility or {}).get("degree_eligible") != "no":
        return None
    degree_re = _degree_pattern()
    if not degree_re:
        return None
    role = (jd_profile or {}).get("role") or ""
    if degree_re.search(role):
        return "the job title names your degree, which contradicts an ineligible verdict"
    quote = (eligibility or {}).get("quoted_requirement") or ""
    if degree_re.search(quote):
        return "the requirement it quoted includes your degree"
    must_have = " ".join((jd_profile or {}).get("must_have_skills") or [])
    if degree_re.search(must_have):
        return "the posting's required qualifications name your degree"
    return None


def should_ask_record(record):
    """Ask approve/skip for this pending record?

    Not asked when ineligible — unless the verdict contradicts itself. The digest
    and the immediate-send path share this one decision.
    """
    prefit = (record or {}).get("prefit") or {}
    if not should_ask(prefit):
        return False
    verdict = ((record or {}).get("eligibility") or {}).get("degree_eligible", "unclear")
    return verdict != "no" or bool(prefit.get("eligibility_override"))


def assess_prefit(backend, jd_profile, db_sections, eligibility, log=print):
    """Judge fit BEFORE asking for approval.

    backend: any llm_client backend. This is light work — a local model does it
    in a few seconds for free — but nothing here requires one.

    Returns {"tier": "high"|"mid"|"low", "reason": str, "unmet": [str],
             "signal": str|None}. On failure it returns "mid": being asked about
    an uncertain posting beats never seeing it.
    """
    jd_profile = jd_profile or {}
    company = jd_profile.get("company") or ""
    role = jd_profile.get("role") or ""
    signal = _pattern_signal(role, company)

    verdict = (eligibility or {}).get("degree_eligible", "unclear")
    contradiction = eligibility_contradicted(jd_profile, eligibility)
    if verdict == "no" and not contradiction:
        # No point asking a model about fit: this path ends in a single
        # "draft it anyway" button either way.
        return {"tier": "low", "reason": "the posting's own requirements rule you out",
                "unmet": [], "signal": signal}
    if contradiction:
        log(f"  [ignoring the eligibility verdict] {contradiction}")

    eligibility_line = {
        "yes": "requirements appear to be met",
        "unclear": "cannot tell from the posting",
    }.get(verdict, "cannot tell from the posting")

    prompt = PREFIT_PROMPT_TEMPLATE.format(
        applicant_block=profile_module.get().applicant_block(),
        db_blob=_compact_db_blob(db_sections),
        company=company or "(unknown)",
        role=role or "(unknown)",
        track=jd_profile.get("track") or "(unknown)",
        must_have=", ".join(jd_profile.get("must_have_skills") or []) or "(not stated)",
        responsibilities="; ".join((jd_profile.get("responsibilities") or [])[:6]) or "(not stated)",
        ideal=(jd_profile.get("ideal_candidate_summary") or "").strip()[:400] or "(not stated)",
        eligibility_line=eligibility_line,
        history_block=_history_block(),
    )

    try:
        parsed = llm_client.parse_json_loose(backend.complete(prompt)) or {}
        tier = parsed.get("tier") if parsed.get("tier") in TIERS else "mid"
        reason = str(parsed.get("reason", "")).strip()[:200]
        unmet = [str(u).strip()[:90] for u in (parsed.get("unmet") or []) if str(u).strip()][:2]
    except Exception as e:
        log(f"  [fit judgment failed — asking anyway (mid)] {e}")
        tier, reason, unmet = "mid", f"could not judge: {e}", []

    clamped, clamp_reason = _clamp(tier, signal)
    if clamp_reason and clamped != tier:
        log(f"  [fit adjusted] {tier} -> {clamped} ({clamp_reason})")
        reason = f"{reason} / {clamp_reason}" if reason else clamp_reason
        tier = clamped

    out = {"tier": tier, "reason": reason, "unmet": unmet, "signal": signal}
    if contradiction:
        # Ineligible, but the verdict contradicted itself — you need to know that
        # this is why you are being asked.
        out["eligibility_override"] = True
        out["reason"] = f"{reason} / {contradiction}" if reason else contradiction
        if _TIER_ORDER[out["tier"]] < _TIER_ORDER["mid"]:
            out["tier"] = "mid"
    log(f"  fit: {out['tier']} — {out['reason']}")
    return out


def should_ask(prefit):
    """Offer approve/skip, or only "draft it anyway"?

    Below config.PREFIT_ASK_MIN_TIER the pair is not offered: for those postings
    ignoring the message *was* the answer, 43 expiries' worth of it.
    """
    tier = (prefit or {}).get("tier", "mid")
    return _TIER_ORDER.get(tier, 1) >= _TIER_ORDER.get(config.PREFIT_ASK_MIN_TIER, 1)


def sort_key(prefit):
    """Digest ordering: best fit first."""
    return -_TIER_ORDER.get((prefit or {}).get("tier", "mid"), 1)
