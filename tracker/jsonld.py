"""schema.org JobPosting, read out of a careers page's own markup.

Most job pages carry a `<script type="application/ld+json">` block describing
the posting, because Google's job search reads it. It is pre-rendered on the
server even on pages whose visible text is assembled by JavaScript, so a plain
GET gets you clean prose plus the facts the page is willing to state outright.

Two things come from it, and both matter here:

1. **Clean description text.** A JS-rendered ATS page scraped naively yields the
   application form's own schema instead of the posting. One real case returned
   "label x2546, questionid x857, choices x541" as its top keywords, and the
   degree screen came back "unclear" because the posting text was never there.

2. **The facts the fit gate needs, without asking a model.** `jobLocation`,
   `employmentType`, `baseSalary`, `experienceRequirements`, `validThrough`.
   A model asked to read a salary range out of prose sometimes invents one; a
   number lifted from the page's own structured data is either there or absent.
   Absent is a fine answer — every axis in fit_gate is skipped when unknown.

Fields the page does not publish come back as None. Nothing here guesses.
"""

import html
import json
import re

import requests

import config

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t ]+")
_NL_RE = re.compile(r"\n{3,}")

_LD_JSON_RE = re.compile(
    r"<script[^>]*type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
    re.S | re.I)

# schema.org spells these in SCREAMING_SNAKE; the rest of this project uses the
# words a posting uses.
_EMPLOYMENT_TYPES = {
    "FULL_TIME": "full_time",
    "FULLTIME": "full_time",
    "PART_TIME": "part_time",
    "PARTTIME": "part_time",
    "CONTRACTOR": "contract",
    "CONTRACT": "contract",
    "TEMPORARY": "contract",
    "INTERN": "internship",
    "INTERNSHIP": "internship",
    "VOLUNTEER": "volunteer",
    "PER_DIEM": "contract",
    "OTHER": "",
}


def _html_to_text(fragment):
    """A JobPosting.description is usually an HTML fragment."""
    text = html.unescape(fragment or "")
    text = re.sub(r"<br\s*/?>", "\n", text)
    text = re.sub(r"</(p|li|div|h[1-6]|tr)>", "\n", text)
    text = _TAG_RE.sub(" ", text)
    # Entities survive one round of tag-stripping (&lt;div&gt; arrives as
    # literal text), so unescape again after the tags are gone.
    text = html.unescape(text)
    text = _WS_RE.sub(" ", text)
    text = _NL_RE.sub("\n\n", text)
    return "\n".join(line.strip() for line in text.splitlines()).strip()


def _iter_objects(node):
    """ld+json arrives as one object, a list, or an @graph. Flatten all three."""
    if isinstance(node, list):
        for item in node:
            yield from _iter_objects(item)
    elif isinstance(node, dict):
        yield node
        graph = node.get("@graph")
        if graph is not None:
            yield from _iter_objects(graph)


def _is_job_posting(obj):
    types = obj.get("@type")
    types = types if isinstance(types, list) else [types]
    return "JobPosting" in types


def _first(value):
    if isinstance(value, list):
        return value[0] if value else None
    return value


def _location(obj):
    """'San Francisco, CA, US' from jobLocation, or 'Remote' when it says so."""
    places = obj.get("jobLocation")
    parts = []
    for place in (places if isinstance(places, list) else [places]):
        if not isinstance(place, dict):
            continue
        address = place.get("address")
        if isinstance(address, str):
            parts.append(address)
            continue
        if not isinstance(address, dict):
            continue
        bits = [address.get("addressLocality"), address.get("addressRegion"),
                address.get("addressCountry")]
        bits = [b.get("name") if isinstance(b, dict) else b for b in bits]
        bits = [str(b).strip() for b in bits if b and str(b).strip()]
        if bits:
            parts.append(", ".join(bits))
    seen, unique = set(), []
    for part in parts:
        if part.lower() not in seen:
            seen.add(part.lower())
            unique.append(part)
    return " / ".join(unique[:3]) or None


def _work_arrangement(obj):
    """remote | onsite, from the one field schema.org has for it.

    `jobLocationType: TELECOMMUTE` is the only arrangement schema.org states.
    Hybrid has no representation, so it is never claimed from here — the model
    reads it out of the prose instead.
    """
    kind = str(_first(obj.get("jobLocationType")) or "").strip().upper()
    if kind == "TELECOMMUTE":
        return "remote"
    return None


def _employment_type(obj):
    raw = obj.get("employmentType")
    for value in (raw if isinstance(raw, list) else [raw]):
        key = str(value or "").strip().upper().replace("-", "_").replace(" ", "_")
        mapped = _EMPLOYMENT_TYPES.get(key)
        if mapped:
            return mapped
    return None


def _comp_range(obj):
    """{'min', 'max', 'currency', 'period'} from baseSalary, or None.

    A posting that publishes one number gets min == max. A posting that
    publishes none gets None, and the compensation axis is then not checked.
    """
    salary = _first(obj.get("baseSalary"))
    if not isinstance(salary, dict):
        return None
    value = salary.get("value")
    if not isinstance(value, dict):
        return None

    def number(*keys):
        for key in keys:
            raw = value.get(key)
            try:
                num = float(raw)
            except (TypeError, ValueError):
                continue
            if num > 0:
                return num
        return None

    low = number("minValue", "value")
    high = number("maxValue", "value") or low
    if not low:
        return None
    unit = str(value.get("unitText") or "").strip().lower()
    period = {"year": "year", "yearly": "year", "annual": "year",
              "month": "month", "monthly": "month",
              "week": "week", "weekly": "week",
              "day": "day", "daily": "day",
              "hour": "hour", "hourly": "hour"}.get(unit, "year")
    currency = str(salary.get("currency") or value.get("currency") or "").strip().upper()
    return {"min": low, "max": high or low, "currency": currency or None, "period": period}


def _years_required(obj):
    """Years of experience, when the page states them as a number.

    schema.org allows either a monthsOfExperience integer or free prose, and the
    prose form ("5+ years in a product role") is not worth parsing here — the
    model reads that. Only the integer is trusted.
    """
    requirements = _first(obj.get("experienceRequirements"))
    if isinstance(requirements, dict):
        months = requirements.get("monthsOfExperience")
        try:
            months = float(months)
        except (TypeError, ValueError):
            return None
        if months > 0:
            return round(months / 12.0, 1)
    return None


def extract(page_html):
    """Every fact the page's JobPosting block states. {} when there is none.

    Keys, all optional: text, title, company, location, work_arrangement,
    employment_type, comp_range, years_required, valid_through.
    """
    best = {}
    for raw in _LD_JSON_RE.findall(page_html or ""):
        try:
            data = json.loads(raw.strip())
        except (ValueError, TypeError):
            continue
        for obj in _iter_objects(data):
            if not _is_job_posting(obj):
                continue
            text = _html_to_text(obj.get("description", ""))
            # Several blocks can describe the same posting (a summary card plus
            # the posting itself). The longest description is the real one.
            if best and len(text) <= len(best.get("text") or ""):
                continue
            organisation = _first(obj.get("hiringOrganization"))
            company = organisation.get("name") if isinstance(organisation, dict) else None
            found = {
                "text": text,
                "title": (obj.get("title") or "").strip() or None,
                "company": (str(company).strip() or None) if company else None,
                "location": _location(obj),
                "work_arrangement": _work_arrangement(obj),
                "employment_type": _employment_type(obj),
                "comp_range": _comp_range(obj),
                "years_required": _years_required(obj),
                "valid_through": (str(obj.get("validThrough") or "").strip() or None),
            }
            best = {k: v for k, v in found.items() if v is not None}
    return best


def extract_text(page_html):
    """Just the posting prose. '' when the page publishes no JobPosting block."""
    return extract(page_html).get("text") or ""


def fetch(job_url):
    """GET the page and read its JobPosting block. {} on any failure."""
    try:
        resp = requests.get(
            job_url,
            headers={"User-Agent": config.HTTP_USER_AGENT},
            timeout=config.HTTP_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"  [jsonld] request failed for {job_url}: {e}")
        return {}
    return extract(resp.text)
