"""Turn a posting into the facts the rest of the pipeline reasons about.

Three kinds of fact come out of here, and they are kept apart on purpose:

- **Facts the page states in machine-readable form** — location, employment
  type, salary range, years of experience — read out of the page's own
  schema.org JobPosting block by tracker.jsonld. These are not guessed and not
  paraphrased; a page either publishes them or it does not.
- **Facts a model reads out of the prose** — the ideal candidate, the skills,
  the vocabulary this company uses for its own work.
- **Facts a regex finds better than a model** — sponsorship exclusions, and
  licence/clearance requirements. Both are boilerplate with a handful of
  variants, and both have been read straight past by a local model in a case
  that mattered.

Nothing here filters. Every verdict travels to the report, and fit_gate decides
what it means for this applicant.
"""

import json
import re

import requests
import trafilatura
from bs4 import BeautifulSoup

import config
import llm_client  # noqa: F401  (kept for callers that import backends from here)
import profile as profile_module
from tracker import jsonld

MIN_JD_LENGTH = 200


def fetch_jd(url):
    """Fetch a posting. Returns {"text": str|None, "facts": dict}.

    Three readers, in order of how much they can be trusted:

    1. the page's schema.org JobPosting block — clean prose plus stated facts
    2. trafilatura — good at finding the article in a content page
    3. BeautifulSoup over the whole document — last resort

    JSON-LD goes first because a JavaScript-rendered ATS page defeats both of
    the others: scraped naively, one such page yielded the application form's
    own field schema ("label x2546, questionid x857") as the posting text, and
    the degree screen reported "unclear" because the requirements were never in
    what it was given.
    """
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": config.HTTP_USER_AGENT},
            timeout=config.HTTP_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"  request failed: {e}")
        return {"text": None, "facts": {}}

    html_body = resp.text
    facts = jsonld.extract(html_body)
    text = facts.pop("text", "") or ""

    if len(text.strip()) < MIN_JD_LENGTH:
        text = trafilatura.extract(html_body, favor_recall=True) or ""

    if len(text.strip()) < MIN_JD_LENGTH:
        soup = BeautifulSoup(html_body, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()
        text = soup.get_text(separator="\n")
        text = re.sub(r"\n{3,}", "\n\n", text).strip()

    if len(text.strip()) < MIN_JD_LENGTH:
        return {"text": None, "facts": facts}

    return {"text": text.strip(), "facts": facts}


def fetch_jd_text_from_url(url):
    """Just the posting text. None if it fails or looks too short."""
    return fetch_jd(url)["text"]


def _parse_json_response(raw_text):
    cleaned = raw_text.strip()
    fence_match = re.search(r"```(?:json)?\s*(.*?)\s*```", cleaned, re.DOTALL)
    if fence_match:
        cleaned = fence_match.group(1)
    return json.loads(cleaned)


JD_PROFILE_SCHEMA_PROMPT = """\
Below is a job posting. Analyse it and answer with this JSON schema and nothing else \
(no commentary, JSON only):

{
  "company": "company name",
  "role": "role title",
  "track": "how this hire is most likely screened — the hiring world this role sits in, in one or two words (Tech, Consulting, VC, PE, Startup, Healthcare, Government, Nonprofit, Academia, Manufacturing, Finance, Retail, General if unsure)",
  "scale": "Startup or Enterprise, whichever the organisation is closer to",
  "employment_type": "one of internship/new_grad/full_time/contract/part_time, or an empty string if the posting does not say",
  "seniority_level": "the level as the posting words it (intern, new grad, associate, mid, senior, staff, principal, director), or an empty string",
  "years_required": "the minimum years of experience the posting states, as a number only, or an empty string if it states none",
  "location": "where the work is, as the posting writes it, or an empty string",
  "work_arrangement": "one of remote/hybrid/onsite, or an empty string if the posting does not say",
  "comp_stated": "the compensation range exactly as written in the posting, or an empty string",
  "start_date": "when the role starts or the term it covers (e.g. 'Summer 2027', 'immediately'), or an empty string",
  "must_have_skills": ["required skills and qualifications"],
  "preferred_skills": ["preferred / nice-to-have skills"],
  "keywords": ["terms the posting repeats or emphasises"],
  "responsibilities": ["what the person will actually do"],
  "culture_values": ["the values or culture the company stresses"],
  "company_vocabulary": ["this company's own names for its products, customers and work — only phrases that literally appear in the posting (e.g. 'enterprise AI applications', 'forward deployed engineering'). No generic nouns"],
  "ideal_candidate_summary": "3-5 sentences on the candidate this posting wants"
}

Every one of these must come from the posting's own words. **An empty string is the
right answer whenever the posting does not say** — a guessed location or an invented
salary range is worse than none, because the tool screens on these.

Keep every skill and keyword in the posting's own words — translating or paraphrasing them
destroys the ATS keyword match, which is what they are for.

Job posting:
---
{jd_text}
---
"""

# The shape every caller can rely on, so a degraded reply is still usable.
_JD_PROFILE_DEFAULTS = {
    "company": "", "role": "", "track": "General", "scale": "",
    "employment_type": "", "seniority_level": "", "years_required": None,
    "location": "", "work_arrangement": "", "comp_stated": "", "start_date": "",
    "must_have_skills": [], "preferred_skills": [], "keywords": [],
    "responsibilities": [], "culture_values": [], "company_vocabulary": [],
    "ideal_candidate_summary": "",
}


def _as_years(value):
    """'5+ years' -> 5.0. '' -> None. Never raises."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return float(value) or None
    match = re.search(r"\d+(?:\.\d+)?", str(value))
    return float(match.group()) if match else None


def extract_jd_profile(backend, jd_text, facts=None):
    """backend: anything from llm_client with .complete(prompt) -> str.

    facts: what the page published about itself (tracker.jsonld). These overwrite
    the model's reading, because they are the posting's own structured claim
    rather than a paraphrase of its prose.

    A malformed reply degrades to the defaults rather than aborting. This used to
    raise straight out of json.loads, which killed the whole posting over one
    stray token — while fit_gate, one call later, had always degraded to
    "possible fit" for exactly the same failure.
    """
    prompt = JD_PROFILE_SCHEMA_PROMPT.replace("{jd_text}", jd_text)
    # The CALL is inside the guard, not just the parsing. A reply that could not
    # be parsed degraded to the posting's own facts, while a reply that never
    # arrived still aborted the whole posting — and the second failure is the
    # common one on a local model: a reasoning model spends its generation
    # budget on thinking and returns an empty answer. Both are "the model did
    # not tell us anything", and neither is worth losing the posting over.
    try:
        raw_text = backend.complete(prompt)
        parsed = _parse_json_response(raw_text)
        if not isinstance(parsed, dict):
            raise ValueError(f"expected an object, got {type(parsed).__name__}")
    except Exception as e:  # noqa: BLE001 — any backend failure degrades the same way
        print(f"  [jd analysis] no usable answer from the model ({e}); "
              "continuing with what the posting itself publishes")
        parsed = {}

    jd_profile = dict(_JD_PROFILE_DEFAULTS)
    for key, default in _JD_PROFILE_DEFAULTS.items():
        value = parsed.get(key, default)
        if isinstance(default, list):
            value = [str(v).strip() for v in (value or []) if str(v).strip()]
        elif key == "years_required":
            value = _as_years(value)
        else:
            value = str(value or "").strip()
        jd_profile[key] = value

    jd_profile["comp_range"] = None
    jd_profile["valid_through"] = ""

    # What the page states about itself wins. `comp_stated` is included because
    # of what the model does without it: handed a posting whose own listing said
    # "$14/hour", it reported "The rate of pay is 0.2333 USD" — a number it had
    # derived rather than read. A string the source published is the posting's
    # own claim; a string a model assembled is not.
    for key in ("location", "work_arrangement", "employment_type", "company",
                "comp_stated"):
        if (facts or {}).get(key):
            jd_profile[key] = facts[key]
    if (facts or {}).get("title") and not jd_profile["role"]:
        jd_profile["role"] = facts["title"]
    if (facts or {}).get("years_required") is not None:
        jd_profile["years_required"] = facts["years_required"]
    if (facts or {}).get("comp_range"):
        jd_profile["comp_range"] = facts["comp_range"]
    if (facts or {}).get("valid_through"):
        jd_profile["valid_through"] = facts["valid_through"]

    return jd_profile


ELIGIBILITY_PROMPT = """\
Below is a job posting. Here is the applicant it is being screened for:

{applicant_block}

In this posting's qualifications / eligibility / requirements, look for anything that
restricts the degree or field of study. Wording like "technical degree", "computer
science", "engineering" or "STEM" restricts it; "any major", "advanced degree" or a
posting that never names a degree type does not. Decide whether THIS applicant's
degree literally satisfies it: "yes", "no", or "unclear" when the posting gives you
nothing to judge by.

**Quote only sentences that literally appear in the posting. Never invent a
requirement that is not written there.**

Answer with this JSON schema and nothing else (no commentary, JSON only):
{
  "degree_eligible": "yes, no, or unclear",
  "quoted_requirement": "the degree/field requirement quoted verbatim from the posting, or an empty string if there is none",
  "reasoning": "1-2 sentences on why",
  "visa_caution": "true or false — true only for an explicit requirement this applicant cannot satisfy (see above)",
  "visa_note": "when visa_caution is true, the quote or a one-sentence explanation; otherwise an empty string"
}

Job posting:
---
{jd_text}
---
"""


# Sponsorship exclusions are found by REGEX, not by the model.
#
# The case that settled it: a posting read "Candidates must be authorized to work in
# the United States without the need for current or future company sponsorship." A
# local model read straight past it and reported no concern — and that sentence was
# the real reason the application could not go anywhere. Where a role is held under a
# student's own authorisation it may need no sponsorship at all, so "current" is often
# fine; "future" is the word that closes the conversion an internship exists to start.
#
# This wording is boilerplate with a handful of variants, so matching it is more
# accurate than asking a model. Whether the match MATTERS to you depends on your
# status, and the report, the digest and the console all consult
# profile.sponsorship_exclusion_matters before warning about it — that sentence is
# in a large share of postings, and for someone who needs no sponsorship it warns
# about nothing. (This comment claimed as much long before anything did it.)
#
# These patterns say nothing about a country: they key off the word "sponsor" and a
# negation, which is how the exclusion is written everywhere it is written in English.
# They run whether or not market.country is set, because the measured failure above is
# too expensive to make conditional on a field someone may leave blank.
_SPONSORSHIP_EXCLUSION_PATTERNS = [
    r"without\s+(?:the\s+)?(?:need|requirement)\s+for[^.]{0,60}sponsor",
    r"\b(?:not|unable|cannot|can\s?not|won'?t|will\s+not|do(?:es)?\s+not)\b[^.]{0,50}\bsponsor",
    r"\bno\s+(?:visa\s+|immigration\s+|employment\s+)?sponsorship\b",
    r"sponsorship\s+(?:is\s+)?not\s+(?:available|offered|provided)",
    r"must\s+(?:already\s+)?be\s+(?:legally\s+)?(?:authorized|eligible)[^.]{0,80}without[^.]{0,40}sponsor",
]

# Phrasings that only exist in one jurisdiction, added when market.country says so.
# An employer in the UK does not write "H-1B"; it writes "Certificate of Sponsorship",
# and a scan carrying only US wording reports a clean posting.
#
# Each entry carries its own TRIGGERS as well as its patterns, and that pairing is
# the point. The scan skips any sentence with none of the trigger words, so that it
# runs a handful of regexes over a handful of sentences rather than all of them over
# all of them — which means a pattern whose vocabulary is not in the trigger list can
# never match. That is not theoretical: "must be legally entitled to work in Canada"
# contains no form of the word "sponsor", so the Canadian patterns were unreachable
# the moment they were written. tests/test_search_axes.py now holds every country's
# patterns to being reachable.
_SPONSORSHIP_TRIGGERS = ("sponsor",)

_SPONSORSHIP_EXCLUSION_BY_COUNTRY = {
    "US": {
        # Triggers are matched against the normalised text, so "us person" covers
        # both "US person" and "U.S. person".
        "triggers": ("sponsor", "citizens only", "us person", "us citizen"),
        "patterns": [
            r"\b(?:h-?1-?b|opt|cpt|ead|tn\s+visa)\b[^.]{0,60}\bnot\b",
            r"\bu\.?s\.?\s+(?:citizens?|persons?)\s+only\b",
        ],
    },
    "GB": {
        "triggers": ("sponsor", "right to work", "skilled worker"),
        "patterns": [
            r"\b(?:not|unable|cannot|do(?:es)?\s+not)\b[^.]{0,60}certificate\s+of\s+sponsorship",
            r"certificate\s+of\s+sponsorship[^.]{0,60}\b(?:not|cannot|unable)\b",
            r"\b(?:must|need)\b[^.]{0,40}existing\s+right\s+to\s+work\s+in\s+the\s+uk",
            r"\bskilled\s+worker\s+(?:visa|route)\b[^.]{0,60}\b(?:not|cannot|unable)\b",
        ],
    },
    "CA": {
        "triggers": ("sponsor", "work in canada", "canadian citizen"),
        "patterns": [
            r"\bmust\s+be\s+(?:legally\s+)?(?:entitled|authorized|eligible)"
            r"\s+to\s+work\s+in\s+canada\b",
            r"\bcanadian\s+citizens?\s+(?:or\s+permanent\s+residents?\s+)?only\b",
        ],
    },
    "SG": {
        "triggers": ("sponsor", "employment pass"),
        # Both word orders. "We cannot provide an Employment Pass" and "an
        # Employment Pass cannot be provided" are the same sentence, and a
        # one-directional pattern catches only the second.
        "patterns": [
            r"\b(?:employment\s+pass)\b[^.]{0,60}\b(?:not|cannot|unable)\b",
            r"\b(?:not|cannot|unable|do(?:es)?\s+not)\b[^.]{0,60}\bemployment\s+pass\b",
        ],
    },
    "AU": {
        "triggers": ("sponsor", "working rights", "work rights"),
        "patterns": [
            r"\bmust\s+have\s+(?:full\s+)?work(?:ing)?\s+rights\s+in\s+australia\b"],
    },
}

# The EU/EEA permit wording is the same in every member state, so these share one
# entry rather than being copied per country.
_EU_PERMIT = {
    "triggers": ("sponsor", "work permit", "work authorisation", "work authorization"),
    "patterns": [
        r"\b(?:eu|eea)\s+work\s+(?:permit|authorisation|authorization)\s+required\b",
        r"\b(?:must|need)\b[^.]{0,40}\b(?:eu|eea)\s+work\s+"
        r"(?:permit|authorisation|authorization)\b",
    ],
}
for _code in ("DE", "NL", "IE", "FR", "ES", "IT", "SE", "DK", "FI", "PL", "AT", "BE", "PT"):
    _SPONSORSHIP_EXCLUSION_BY_COUNTRY[_code] = _EU_PERMIT

# These in the same sentence mean the opposite: they do sponsor.
_SPONSORSHIP_POSITIVE_PATTERNS = [
    r"\b(?:will|do(?:es)?|can|may)\s+sponsor\b",
    r"sponsorship\s+(?:is\s+)?(?:available|offered|provided)",
    r"track\s+record\s+of\s+(?:offering|providing)",
    r"\bwe\s+sponsor\b",
    r"certificate\s+of\s+sponsorship\s+(?:is\s+)?(?:available|offered|provided)",
]


def sponsorship_rules(country=None):
    """(triggers, patterns) for one job market.

    The country-neutral set always applies. A country adds its own wording on
    top, and its triggers with it — see the comment above
    `_SPONSORSHIP_EXCLUSION_BY_COUNTRY` for why those travel together.
    """
    if country is None:
        country = profile_module.get().market_country
    local = _SPONSORSHIP_EXCLUSION_BY_COUNTRY.get((country or "").upper(), {})
    triggers = tuple(dict.fromkeys(_SPONSORSHIP_TRIGGERS + tuple(local.get("triggers", ()))))
    return triggers, _SPONSORSHIP_EXCLUSION_PATTERNS + list(local.get("patterns", []))


# Sentence splitting, with one guard: do not split inside a dotted initialism.
#
# A plain split on "a full stop followed by whitespace" cuts "open to U.S.
# citizens only" into "open to U.S." and "citizens only" — and then a pattern
# that needs both halves cannot match, which is how the US citizens-only check
# came to be dead on arrival. The lookbehind refuses to split when the four
# characters before the space look like `X.Y.`, so "U.S." and "E.U." hold
# together while "USA. Candidates" still splits.
#
# This errs towards joining two sentences, which is the safe direction: a joined
# sentence is still scanned and only widens the quote, whereas a wrongly split
# one loses the match entirely.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])(?<![A-Z]\.[A-Z]\.)\s+|\n+")


def sentences(text):
    """The posting's sentences, for the scans that read one sentence at a time."""
    for sentence in _SENTENCE_SPLIT_RE.split(text or ""):
        sentence = sentence.strip()
        if sentence:
            yield sentence


# Dotted initialisms, collapsed before matching: "u.s." -> "us", "e.u." -> "eu".
#
# The patterns below bound their gaps with `[^.]{0,80}` so a match cannot run
# past the end of a sentence. A dotted abbreviation inside the sentence is then
# an impassable wall: "must be authorized to work in the U.S. without the need
# for sponsorship" is one sentence and one requirement, and no gap could cross
# it. Collapsing the dots fixes that for every pattern at once rather than
# teaching each one about abbreviations, and the patterns spell the dots
# optionally (`u\.?s\.?`) so both forms still match.
_INITIALISM_RE = re.compile(r"\b(?:[a-z]\.){2,}")


def _for_matching(sentence):
    low = sentence.lower()
    return _INITIALISM_RE.sub(lambda m: m.group().replace(".", ""), low)


def detect_sponsorship_exclusion(jd_text, country=None):
    """Find a sentence saying candidates who need sponsorship are not eligible.

    Returns {"excluded": bool, "quote": str}, the quote being the sentence itself
    (300 chars max). Sentences ending in a question mark are skipped: an
    application form's own question ("Do you now or will you in the future
    require immigration sponsorship?") is not an exclusion.

    country: overrides profile.yaml's market, for tests and for checking one
    posting against another market's phrasebook.
    """
    triggers, patterns = sponsorship_rules(country)
    for sentence in sentences(jd_text):
        if sentence.endswith("?"):
            continue
        low = _for_matching(sentence)
        # Cheap gate: most sentences in a posting are about the job.
        if not any(trigger in low for trigger in triggers):
            continue
        # Only a sentence with no negation counts as "we do sponsor" — otherwise
        # "No visa sponsorship is available" matches the positive pattern
        # ("sponsorship is available") and escapes the check.
        negated = re.search(r"\b(?:no|not|unable|cannot|can\s?not|won'?t|without|only)\b", low)
        if not negated and any(re.search(p, low) for p in _SPONSORSHIP_POSITIVE_PATTERNS):
            continue
        if any(re.search(p, low) for p in patterns):
            return {"excluded": True, "quote": sentence[:300]}
    return {"excluded": False, "quote": ""}


# A clearance or a licence is a requirement no résumé edit can satisfy, and it reads
# like ordinary prose, so a model weighs it as one more preferred skill. Counting the
# phrasings is both cheaper and more reliable, exactly as with sponsorship.
_HARD_CREDENTIAL_PATTERNS = (
    # A citizenship or export-control residency bar is NOT a sponsorship
    # exclusion, and the two are kept apart on purpose: a sponsorship line
    # closes the long-term path while the role itself may still be open,
    # whereas "U.S. Person as defined by ITAR" closes the role outright. It is
    # counted here rather than left to the model for the same reason
    # sponsorship is — it reads as ordinary boilerplate, so a model weighs it
    # as one more preferred qualification.
    ("citizenship or residency status",
     r"\b(?:u\.?s\.?|us)\s+persons?\b|\bitar\b|\bexport\s+control"
     r"(?:led|s)?\b[^.]{0,40}\b(?:person|citizen|national)\b|"
     r"\b(?:citizens?|nationals?)\s+only\b|"
     r"\bmust\s+be\s+a\s+(?:citizen|national)\b"),
    ("security clearance",
     r"\b(?:active|current|existing|ability to obtain)?\s*(?:ts/sci|top\s+secret|secret|"
     r"security|government|public\s+trust)\s+clearance\b"),
    ("professional licence",
     r"\b(?:licensed|licence|license|registration)\s+(?:as|required|in good standing)\b|"
     r"\b(?:cpa|pe|rn|md|jd|bar)\s+(?:licence|license|certification|admission)\s+required\b"),
    ("driving licence",
     r"\b(?:valid\s+)?driver'?s?\s+(?:licence|license)\b"),
    ("certification",
     r"\b(?:certification|certified)\s+(?:is\s+)?required\b"),
)


def detect_hard_credentials(jd_text):
    """Credential requirements no résumé edit can satisfy. [] when there are none.

    Each entry is {"kind", "quote"}. Reported, never used to block: a posting that
    merely prefers a clearance is written the same way as one that demands it, and
    the applicant is the one who knows whether they hold it.
    """
    found, seen = [], set()
    for sentence in sentences(jd_text):
        if sentence.endswith("?"):
            continue
        low = _for_matching(sentence)
        for kind, pattern in _HARD_CREDENTIAL_PATTERNS:
            if kind in seen or not re.search(pattern, low):                continue
            seen.add(kind)
            found.append({"kind": kind, "quote": sentence[:300]})
    return found


def assess_eligibility(backend, jd_text):
    """Does this posting's stated requirements literally admit you?

    Judged from quotes in the posting only, never from inference. It runs before
    the expensive rewriting, because a posting that asks for a degree you do not
    have is not worth a draft — the case that prompted it was an internship whose
    requirement was a "technical degree program", found only after a full draft
    had been generated.

    This never hard-blocks. The verdict and the quote go into the report and you
    decide: the pipeline's rule is that a wrong "no" costs more than a wasted
    draft (see fit_gate.eligibility_contradicted, which catches the verdicts that
    contradict themselves).
    """
    prompt = ELIGIBILITY_PROMPT.replace(
        "{applicant_block}", profile_module.get().eligibility_block()
    ).replace("{jd_text}", jd_text)
    try:
        raw_text = backend.complete(prompt)
        result = _parse_json_response(raw_text)
        if not isinstance(result, dict):
            raise ValueError(f"expected an object, got {type(result).__name__}")
    except Exception as e:  # noqa: BLE001 — see extract_jd_profile
        # "unclear" is the honest verdict for an answer nobody could read, and it
        # is also the one that changes nothing downstream. The regex scans below
        # still run, and they are the ones that catch the hard blockers.
        print(f"  [eligibility] no usable answer from the model ({e}); "
              "recording the verdict as unclear")
        result = {"reasoning": f"the model gave no usable answer: {e}"}
    result.setdefault("degree_eligible", "unclear")
    result.setdefault("quoted_requirement", "")
    result.setdefault("reasoning", "")
    result["visa_caution"] = bool(result.get("visa_caution"))
    result.setdefault("visa_note", "")
    # Sponsorship exclusions and credential requirements are found by regex, not by
    # the model: see detect_sponsorship_exclusion and detect_hard_credentials.
    sponsorship = detect_sponsorship_exclusion(jd_text)
    result["sponsorship_excluded"] = sponsorship["excluded"]
    result["sponsorship_quote"] = sponsorship["quote"]
    result["hard_credentials"] = detect_hard_credentials(jd_text)
    # One place to read "what this posting demands that a résumé cannot change".
    # fit_gate fills in the seniority and location verdicts, which are arithmetic
    # against profile.yaml rather than a reading of the prose.
    result["requirements"] = {
        "degree": result["degree_eligible"],
        "authorization": "no" if (result["visa_caution"] or result["sponsorship_excluded"])
                         else "unclear",
        "credentials": [c["kind"] for c in result["hard_credentials"]],
    }
    return result
