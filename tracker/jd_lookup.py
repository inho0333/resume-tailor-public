"""Getting a posting's body out of its URL, cheapest route first.

An aggregator's listing has a title and a link. Everything downstream — the
eligibility screen, the fit judgment, the ATS keywords, the edit itself — reads
the body, so a posting with no text is a posting that cannot be judged.

The ladder, in order:

    1. the company's own ATS API, when the URL names one
       (Greenhouse, Lever, Ashby) — authoritative, clean, no scraping
    2. the page's schema.org JobPosting block — clean prose plus stated facts
    3. the page's visible text — trafilatura, then the whole document

Each rung is tried only if the one above produced too little. Most aggregator
links land on rung 1, because most employers use one of those three.

There is no browser rung. A headless browser would catch the single-page
applications the static rungs cannot read, and the private version of this
pipeline has one — but it costs a Playwright install, a browser download and
4-8 seconds per posting, which is a lot to add to `pip install -r
requirements.txt` for the last few percent. A posting whose body cannot be read
is reported as such and still reaches you with its link; it just arrives without
a fit judgment worth trusting.
"""

import re

from tracker import jsonld

# Below this, "extraction succeeded" is not true. Measured on JavaScript-rendered
# pages: scraping one often yields the `<title>` tag and nothing else — 114, 153
# and 157 characters on three real pages — which is enough to look like success
# and nowhere near enough to judge a posting on. The threshold is deliberately
# the same as the minimum the drafting pipeline needs.
MIN_USEFUL_LENGTH = 200

# A structured block is trustworthy, but some sites put a one-line teaser in it,
# and then the page's visible text is the better source. Measured: real blocks
# ran 953 and 4,270 characters.
MIN_JSONLD_LENGTH = 300

_GREENHOUSE_EMBED_RE = re.compile(r"greenhouse\.io/embed/job_app\?for=([^&]+)&token=(\d+)")
_GREENHOUSE_DIRECT_RE = re.compile(r"greenhouse\.io/(?:embed/)?([^/?]+)/jobs/(\d+)")
_LEVER_RE = re.compile(r"jobs\.lever\.co/([^/]+)/([0-9a-fA-F-]+)")
_ASHBY_RE = re.compile(r"(?:jobs\.ashbyhq\.com|ashbyhq\.com/[^/]+)/([^/?#]+)/([0-9a-fA-F-]{36})")

# Tokens that mean the extractor captured the application FORM's field schema
# instead of the posting. Measured: one JavaScript-rendered page produced 3,956
# hits and real postings produced zero, so the threshold has two orders of
# magnitude of headroom. Without this check, that JSON became the "posting" —
# its top keywords were "label", "questionid", "choices", and the eligibility
# screen reported "unclear" because the requirements were never in the text.
_FORM_SCHEMA_TOKENS = ("questionid", "choices", "isrequired", "ispublic",
                       "fieldtype", "autofill", "placeholder", "maxlength")
_FORM_SCHEMA_THRESHOLD = 20


def looks_like_form_schema(text):
    lowered = (text or "").lower()
    return sum(lowered.count(t) for t in _FORM_SCHEMA_TOKENS) >= _FORM_SCHEMA_THRESHOLD


def fetch(url):
    """Returns {"text": str, "facts": dict, "via": str}.

    `via` names the rung that worked — "greenhouse", "lever", "ashby", "jsonld",
    "page", or "none" — and goes into the report, because "this posting was
    judged on text scraped from a page" is a different confidence level from
    "this came from the employer's own API".
    """
    if not url:
        return {"text": "", "facts": {}, "via": "none"}

    # Imported here rather than at module scope: jd_extractor imports
    # tracker.jsonld, so a top-level import would close the cycle.
    from tracker.sources import ats_ashby, ats_greenhouse, ats_lever

    match = _GREENHOUSE_EMBED_RE.search(url) or _GREENHOUSE_DIRECT_RE.search(url)
    if match:
        text = ats_greenhouse.fetch_single(match.group(1), match.group(2))
        if len(text) >= MIN_USEFUL_LENGTH:
            return {"text": text, "facts": {}, "via": "greenhouse"}

    match = _LEVER_RE.search(url)
    if match:
        text = ats_lever.fetch_single(match.group(1), match.group(2))
        if len(text) >= MIN_USEFUL_LENGTH:
            return {"text": text, "facts": {}, "via": "lever"}

    match = _ASHBY_RE.search(url)
    if match:
        text = ats_ashby.fetch_single(match.group(1), match.group(2))
        if len(text) >= MIN_USEFUL_LENGTH:
            return {"text": text, "facts": {}, "via": "ashby"}

    facts = jsonld.fetch(url)
    text = facts.pop("text", "") or ""
    if len(text.strip()) >= MIN_JSONLD_LENGTH and not looks_like_form_schema(text):
        return {"text": text.strip(), "facts": facts, "via": "jsonld"}

    # Last static rung: whatever the page shows. jd_extractor owns that reader
    # already, and sharing it means one place handles the user agent, the
    # timeout and the trafilatura/BeautifulSoup fallback.
    import jd_extractor

    fetched = jd_extractor.fetch_jd(url)
    page_text = fetched.get("text") or ""
    if len(page_text.strip()) >= MIN_USEFUL_LENGTH:
        if looks_like_form_schema(page_text):
            print(f"  [jd_lookup] {url} returned an application form schema, not a posting")
            return {"text": "", "facts": facts or fetched.get("facts") or {}, "via": "none"}
        return {"text": page_text.strip(),
                "facts": {**(facts or {}), **(fetched.get("facts") or {})},
                "via": "page"}

    return {"text": text.strip() if text else "", "facts": facts, "via": "none"}


def fill(posting):
    """Give a posting its body, if it does not have one. Returns the posting.

    A source that already ships the body (every direct ATS source) is left
    alone — refetching it would be a second request for text already in hand.
    """
    if len(posting.text) >= MIN_USEFUL_LENGTH or not posting.url:
        return posting
    found = fetch(posting.url)
    if found["text"]:
        posting.text = found["text"]
    if found["facts"]:
        # The source's own facts win: it knows its own listing better than a
        # page's markup does.
        posting.facts = {**found["facts"], **(posting.facts or {})}
    posting.facts.setdefault("jd_via", found["via"])
    return posting
