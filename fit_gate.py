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

## Two kinds of judgment, kept apart

Evidence coverage is a reading of the posting against a career, and a model does
it well. Whether the posting is in a city you would move to, pays above your
floor, wants eight years when you have three, or is an internship when you want
a full-time role is **arithmetic** — and a model asked to weigh it alongside
everything else quietly discounts it. So those axes are checked in code
(`check_hard_axes`), each mismatch is named in the message, and the model is
left with the one question it is good at.

Every axis is skipped when either side is unknown. An unstated salary is not a
violation; a profile with no locations filters nothing. This is the same
fail-open rule as the rest of the file: missing a good posting costs more than a
noisy notification.

## Not trusting the model

A small local model, given the same posting three times, returned "low" once,
reasoning "this person does not have an MBA" — because the prompt had not told
it the degree. Hence the guards:
  - a title matching one of your `target_roles` cannot be pushed below "mid";
    missing a posting you wanted costs more than one noisy notification
  - a title matching `avoid_roles` is capped at "mid" however much the model
    likes it
  - "high" requires the model to agree with your own targets — found in the
    title, or failing that in the posting's responsibilities and requirements
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


def _avoid_clause():
    """What "not what the applicant is looking for" means, for this applicant.

    This sentence used to name shift work, field operations and pure
    administration. That is one person's taste, and it shipped to everyone —
    including people searching for exactly the operations roles it down-ranked.
    It now says only what the profile says, and nothing when the profile is
    silent.
    """
    prof = profile_module.get()
    unwanted = list(prof.dealbreakers) + list(prof.avoid_roles)
    if not unwanted:
        return ""
    return " (they have said they will not take: " + ", ".join(unwanted[:6]) + ")"


PREFIT_PROMPT_TEMPLATE = """\
You are screening {search_phrase} for one applicant. Decide whether this \
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
posting, or the nature of the role is not what the applicant is looking \
for{avoid_clause}.

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


def _body_blob(jd_profile):
    """The posting's own description of the work, for role matching."""
    jd_profile = jd_profile or {}
    parts = list(jd_profile.get("responsibilities") or [])
    parts += list(jd_profile.get("must_have_skills") or [])
    parts.append(jd_profile.get("ideal_candidate_summary") or "")
    return " ".join(str(p) for p in parts).lower()


def _pattern_signal(role, company="", body=""):
    """Deterministic signal from your own targets. "strong" | "body" | "weak" | None.

    "strong" is a title match, and it is the only one that can rescue a posting
    the model tiered low — the title is what you would have recognised yourself.

    "body" is a target role found in the responsibilities or requirements but not
    in the title, and it exists because of what the title-only rule did to a
    career-changer: "high" was reachable only when a `target_roles` string
    appeared in the job title, so anyone whose next job is not titled the way
    their last one was had every posting flattened to "possible fit", and the
    digest's ordering carried no information at all. A body match lets a posting
    reach "high"; it does not raise a low one, because the model and the title
    both declined to call it a match.
    """
    strong, weak = _role_patterns()
    title_blob = f"{role or ''} {company or ''}".lower()
    for p in strong:
        if re.search(p, title_blob):
            return "strong"
    for p in weak:
        if re.search(p, title_blob):
            return "weak"
    for p in strong:
        if body and re.search(p, body):
            return "body"
    return None


# ── the axes that are arithmetic, not judgment ──────────────────────────────
#
# A search kind and the employment type a posting states are not always spelled
# the same way and do not always conflict: a new-graduate search is served by a
# full-time posting, and a full-time search is served by a posting that happens
# to be labelled "new grad". Only the pairs left out of this map are mismatches.
_COMPATIBLE_EMPLOYMENT = {
    "internship": {"internship"},
    "new_grad": {"new_grad", "full_time"},
    "full_time": {"full_time", "new_grad"},
    "contract": {"contract", "part_time"},
}

_SENIORITY_ORDER = ("intern", "new grad", "new_grad", "junior", "associate", "mid",
                    "senior", "staff", "principal", "director")

_PERIODS_PER_YEAR = {"year": 1, "month": 12, "week": 52, "day": 260, "hour": 2080}

# A posting asking for one more year than you have is not a mismatch; postings
# round their own requirement up. Two is where "we mean it" starts.
_YEARS_TOLERANCE = 2


def _normalise_place(text):
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def _location_mismatch(prof, jd_profile):
    wanted = [_normalise_place(p) for p in prof.locations]
    where = _normalise_place(jd_profile.get("location"))
    if not wanted or not where:
        return None
    arrangement = (jd_profile.get("work_arrangement") or "").lower()
    if arrangement == "remote" or "remote" in where or "anywhere" in where:
        # A remote role is in every location you named.
        return None
    for place in wanted:
        if not place:
            continue
        # Either direction: "london" matches "London, GB", and "san francisco
        # bay area" is matched by a posting in "San Francisco".
        if place in where or where in place:
            return None
        if any(word in where.split() for word in place.split() if len(word) > 3):
            return None
    if prof.relocate:
        return None
    return (f"the role is in {jd_profile.get('location')}, "
            f"not in {', '.join(prof.locations)}, and you are not relocating")


def _as_annual(amount, period):
    return float(amount) * _PERIODS_PER_YEAR.get(period or "year", 1)


_EARLY_CAREER_LEVELS = ("intern", "new grad", "new_grad", "junior", "associate")


def _years_contradicted(jd_profile):
    """Does the posting disagree with its own years requirement?

    A stated minimum is often boilerplate carried over from another template:
    one posting titled "... Intern" asked for eight years in its requirements
    block. The degree axis already refuses to disqualify on a self-contradicting
    verdict (see eligibility_contradicted); this is the same rule for years,
    which is the axis that matters to every experienced-hire search.
    """
    level = (jd_profile.get("seniority_level") or "").lower().strip()
    title = (jd_profile.get("role") or "").lower()
    employment = (jd_profile.get("employment_type") or "").lower()
    if level in _EARLY_CAREER_LEVELS or employment in ("internship", "new_grad"):
        return True
    return any(word in title for word in ("intern", "new grad", "graduate programme",
                                          "graduate program", "entry level", "entry-level"))


def check_hard_axes(jd_profile, prof=None):
    """Requirements and preferences that a model should not be asked to weigh.

    Returns a list of {"axis", "message"}. Empty when nothing conflicts, which
    includes every case where either side is silent: a posting that states no
    salary and a profile with no floor produce no violation, because "unknown"
    is not "wrong".
    """
    jd_profile = jd_profile or {}
    prof = prof or profile_module.get()
    out = []

    def add(axis, message):
        out.append({"axis": axis, "message": message})

    stated_type = (jd_profile.get("employment_type") or "").lower()
    if prof.search_kind and stated_type:
        allowed = _COMPATIBLE_EMPLOYMENT.get(prof.search_kind, {prof.search_kind})
        if stated_type not in allowed:
            # "full_time" reads as "full time" without this; the hyphen is how
            # every posting spells it, and this string is shown to the user.
            spelled = stated_type.replace("_", "-")
            article = "an" if spelled[0] in "aeiou" else "a"
            add("employment_type",
                f"this is {article} {spelled} role; "
                f"you are looking for {prof.search_phrase}")

    where = _location_mismatch(prof, jd_profile)
    if where:
        add("location", where)

    arrangement = (jd_profile.get("work_arrangement") or "").lower()
    if prof.work_arrangement and arrangement and arrangement not in prof.work_arrangement:
        add("work_arrangement",
            f"the role is {arrangement}; you want {', '.join(prof.work_arrangement)}")

    required = jd_profile.get("years_required")
    # effective_years, not years_experience: a student or new graduate screens as
    # zero rather than as "do not check". See Profile.effective_years.
    have = prof.effective_years
    if required and have is not None:
        if float(required) - _YEARS_TOLERANCE > have:
            if _years_contradicted(jd_profile):
                # The same rescue the degree axis gets: a posting that
                # contradicts itself is not allowed to disqualify you on the
                # strength of the half that reads worse.
                add_note = f"it states {required:g} years but is titled for an early-career " \
                           f"hire, so the requirement is not believed"
                out.append({"axis": "years_experience_ignored", "message": add_note,
                            "advisory": True})
            else:
                had = "no professional experience yet" if not have else f"{have}"
                add("years_experience",
                    f"it asks for {required:g} years; you have {had}")

    level = (jd_profile.get("seniority_level") or "").lower().strip()
    if prof.seniority and level in _SENIORITY_ORDER:
        wanted = [s for s in prof.seniority if s in _SENIORITY_ORDER]
        if wanted:
            gap = min(abs(_SENIORITY_ORDER.index(level) - _SENIORITY_ORDER.index(w))
                      for w in wanted)
            if gap >= 3:
                add("seniority",
                    f"it is pitched at {level}; you are looking at {', '.join(wanted)}")

    floor, offered = prof.comp_floor, jd_profile.get("comp_range")
    if floor and offered and offered.get("max"):
        same_currency = (not offered.get("currency")
                         or offered["currency"] == floor["currency"])
        if same_currency:
            top = _as_annual(offered["max"], offered.get("period"))
            want = _as_annual(floor["amount"], floor.get("period"))
            if top < want:
                currency = f"{offered['currency']} " if offered.get("currency") else ""
                add("compensation",
                    f"the top of its range is {currency}{offered['max']:,.0f} per "
                    f"{offered.get('period') or 'year'}, below your floor of "
                    f"{floor['currency']} {floor['amount']:,.0f}")
    return out


def _history_block():
    lines = decisions.fewshot_lines(max_positive=6, max_negative=6)
    if not lines:
        return "\n"
    return ("\n[What this applicant actually decided before] (postings they drafted or skipped "
            "— the evidence of their taste)\n"
            + "\n".join(f"- {l}" for l in lines) + "\n")


def _clamp(tier, signal, violations=()):
    """Apply the floor, the ceiling, and the hard mismatches.

    Order matters. The title floor runs first, then the hard axes override it:
    "the title matches a role you apply to" is a guess about your taste, while
    "this role is in a city you said you would not move to" is a fact about the
    posting, and the fact wins.
    """
    reason = None
    if signal == "strong" and _TIER_ORDER[tier] < _TIER_ORDER["mid"]:
        tier, reason = "mid", "title matches a role you do apply to"
    elif signal == "weak" and _TIER_ORDER[tier] > _TIER_ORDER["mid"]:
        tier, reason = "mid", "title matches a role you have never chosen"
    elif tier == "high" and signal not in ("strong", "body"):
        # Small models hand out "high" freely. Measured on one batch of 24
        # pending postings: two operations-analyst internships both came back
        # "high", and every posting of that kind had expired unanswered. "high"
        # needs the model and your own targets to agree — in the title, or at
        # least in what the posting says the work is.
        tier, reason = "mid", "no target-role match in the posting, so high -> mid"

    violations = [v for v in violations if not v.get("advisory")]
    if len(violations) >= 2:
        if tier != "low":
            tier, reason = "low", f"{len(violations)} of your own requirements do not match"
    elif violations:
        if _TIER_ORDER[tier] > _TIER_ORDER["mid"]:
            tier, reason = "mid", violations[0]["message"]
    return tier, reason


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
             "violations": [{"axis", "message"}], "signal": str|None}. On failure
    it returns "mid": being asked about an uncertain posting beats never seeing
    it.
    """
    jd_profile = jd_profile or {}
    prof = profile_module.get()
    company = jd_profile.get("company") or ""
    role = jd_profile.get("role") or ""
    signal = _pattern_signal(role, company, _body_blob(jd_profile))
    violations = check_hard_axes(jd_profile, prof)
    for violation in violations:
        log(f"  [your own requirement] {violation['message']}")

    verdict = (eligibility or {}).get("degree_eligible", "unclear")
    contradiction = eligibility_contradicted(jd_profile, eligibility)
    if verdict == "no" and not contradiction:
        # No point asking a model about fit: this path ends in a single
        # "draft it anyway" button either way.
        return {"tier": "low", "reason": "the posting's own requirements rule you out",
                "unmet": [], "violations": violations, "signal": signal}
    if contradiction:
        log(f"  [ignoring the eligibility verdict] {contradiction}")

    eligibility_line = {
        "yes": "requirements appear to be met",
        "unclear": "cannot tell from the posting",
    }.get(verdict, "cannot tell from the posting")

    prompt = PREFIT_PROMPT_TEMPLATE.format(
        search_phrase=prof.search_phrase,
        avoid_clause=_avoid_clause(),
        applicant_block=prof.applicant_block(),
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

    clamped, clamp_reason = _clamp(tier, signal, violations)
    if clamp_reason and clamped != tier:
        log(f"  [fit adjusted] {tier} -> {clamped} ({clamp_reason})")
        reason = f"{reason} / {clamp_reason}" if reason else clamp_reason
        tier = clamped

    out = {"tier": tier, "reason": reason, "unmet": unmet,
           "violations": violations, "signal": signal}
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
