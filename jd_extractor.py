import json
import re

import requests
import trafilatura
from bs4 import BeautifulSoup

import config
import llm_client
import profile as profile_module

MIN_JD_LENGTH = 200


def fetch_jd_text_from_url(url):
    """Extract the posting text from a URL. None if it fails or looks too short."""
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": config.HTTP_USER_AGENT},
            timeout=config.HTTP_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"  request failed: {e}")
        return None

    html = resp.text

    text = trafilatura.extract(html, favor_recall=True)
    if not text or len(text.strip()) < MIN_JD_LENGTH:
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()
        text = soup.get_text(separator="\n")
        text = re.sub(r"\n{3,}", "\n\n", text).strip()

    if not text or len(text.strip()) < MIN_JD_LENGTH:
        return None

    return text.strip()


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
  "track": "how this hire is most likely screened — one of Consulting/Tech/VC/PE/Startup/General (General if unsure)",
  "scale": "Startup or Enterprise, whichever the organisation is closer to",
  "must_have_skills": ["required skills and qualifications"],
  "preferred_skills": ["preferred / nice-to-have skills"],
  "keywords": ["terms the posting repeats or emphasises"],
  "responsibilities": ["what the person will actually do"],
  "culture_values": ["the values or culture the company stresses"],
  "company_vocabulary": ["this company's own names for its products, customers and work — only phrases that literally appear in the posting (e.g. 'enterprise AI applications', 'forward deployed engineering'). No generic nouns"],
  "ideal_candidate_summary": "3-5 sentences on the candidate this posting wants"
}

Keep every skill and keyword in the posting's own words — translating or paraphrasing them
destroys the ATS keyword match, which is what they are for.

Job posting:
---
{jd_text}
---
"""


def extract_jd_profile(backend, jd_text):
    """backend: anything from llm_client with .complete(prompt) -> str."""
    prompt = JD_PROFILE_SCHEMA_PROMPT.replace("{jd_text}", jd_text)
    raw_text = backend.complete(prompt)
    return _parse_json_response(raw_text)


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
# the real reason the application could not go anywhere. An internship done on a
# student visa may need no sponsorship at all, so "current" is often fine; "future"
# is the word that closes the post-graduation conversion a summer internship exists
# to start.
#
# This wording is boilerplate with a handful of variants, so matching it is more
# accurate than asking a model. Whether the match MATTERS to you depends on your
# status, which is why the report consults profile.sponsorship_needed_later.
_SPONSORSHIP_EXCLUSION_PATTERNS = [
    r"without\s+(?:the\s+)?(?:need|requirement)\s+for[^.]{0,60}sponsor",
    r"\b(?:not|unable|cannot|can\s?not|won'?t|will\s+not|do(?:es)?\s+not)\b[^.]{0,50}\bsponsor",
    r"\bno\s+(?:visa\s+|immigration\s+|employment\s+)?sponsorship\b",
    r"sponsorship\s+(?:is\s+)?not\s+(?:available|offered|provided)",
    r"must\s+(?:already\s+)?be\s+(?:legally\s+)?(?:authorized|eligible)[^.]{0,80}without[^.]{0,40}sponsor",
]
# These in the same sentence mean the opposite: they do sponsor.
_SPONSORSHIP_POSITIVE_PATTERNS = [
    r"\b(?:will|do(?:es)?|can|may)\s+sponsor\b",
    r"sponsorship\s+(?:is\s+)?(?:available|offered|provided)",
    r"track\s+record\s+of\s+(?:offering|providing)",
    r"\bwe\s+sponsor\b",
]


def detect_sponsorship_exclusion(jd_text):
    """Find a sentence saying candidates who need sponsorship are not eligible.

    Returns {"excluded": bool, "quote": str}, the quote being the sentence itself
    (300 chars max). Sentences ending in a question mark are skipped: an
    application form's own question ("Do you now or will you in the future
    require immigration sponsorship?") is not an exclusion.
    """
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", jd_text or ""):
        sentence = sentence.strip()
        if not sentence or "sponsor" not in sentence.lower() or sentence.endswith("?"):
            continue
        low = sentence.lower()
        # Only a sentence with no negation counts as "we do sponsor" — otherwise
        # "No visa sponsorship is available" matches the positive pattern
        # ("sponsorship is available") and escapes the check.
        negated = re.search(r"\b(?:no|not|unable|cannot|can\s?not|won'?t|without)\b", low)
        if not negated and any(re.search(p, low) for p in _SPONSORSHIP_POSITIVE_PATTERNS):
            continue
        if any(re.search(p, low) for p in _SPONSORSHIP_EXCLUSION_PATTERNS):
            return {"excluded": True, "quote": sentence[:300]}
    return {"excluded": False, "quote": ""}


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
    raw_text = backend.complete(prompt)
    result = _parse_json_response(raw_text)
    result.setdefault("degree_eligible", "unclear")
    result.setdefault("quoted_requirement", "")
    result.setdefault("reasoning", "")
    result["visa_caution"] = bool(result.get("visa_caution"))
    result.setdefault("visa_note", "")
    # Sponsorship exclusions are found by regex, not by the model: see
    # detect_sponsorship_exclusion.
    sponsorship = detect_sponsorship_exclusion(jd_text)
    result["sponsorship_excluded"] = sponsorship["excluded"]
    result["sponsorship_quote"] = sponsorship["quote"]
    return result
