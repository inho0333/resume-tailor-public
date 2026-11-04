"""Company research (READ) — a port of the READ stage of the original system design.

Shows the candidate a short summary of only the things that would genuinely affect their
application strategy (red flags, organisational change, signals of a hiring surge or freeze).
Employee-review sites such as Glassdoor are left out — scraping them raises ToS problems and
they sit behind a login wall — so this uses only the Google News RSS feed, which needs no API
key. If even that fails it is skipped silently (it is supplementary information and must not
block the whole pipeline).

It is never injected into the bullet rewrite prompt — external news facts about a company
bleeding into the candidate's own experience bullets would risk breaking the "never invent
facts" rule. It is used strictly as information for a human to read (console output + saved
into jd_profile.json + a Telegram alert when notable).
"""

import json
import re
import xml.etree.ElementTree as ET

import requests

import config

GOOGLE_NEWS_RSS = "https://news.google.com/rss/search"


def _fetch_news_headlines(company, max_items=8):
    try:
        resp = requests.get(
            GOOGLE_NEWS_RSS,
            params={"q": company, "hl": "en-US", "gl": "US", "ceid": "US:en"},
            headers={"User-Agent": config.HTTP_USER_AGENT},
            timeout=config.HTTP_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"  [company research] news lookup failed (skipping): {e}")
        return []

    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError:
        return []

    headlines = []
    for item in root.iter("item"):
        title = item.findtext("title")
        if title:
            headlines.append(title.strip())
        if len(headlines) >= max_items:
            break
    return headlines


def _parse_json_response(raw_text):
    cleaned = raw_text.strip()
    fence_match = re.search(r"```(?:json)?\s*(.*?)\s*```", cleaned, re.DOTALL)
    if fence_match:
        cleaned = fence_match.group(1)
    return json.loads(cleaned)


RESEARCH_PROMPT_TEMPLATE = """\
Below are recent news headlines about the company "{company}" (there may be none).

{news_blob}

Review this information and summarise briefly only what would genuinely affect the strategy
for applying to this company — red flags (lawsuits, scandals, large layoffs), major
organisational change (M&A, leadership turnover), signals of a hiring surge or freeze, publicly
reported culture issues, and the like. Do not speculate beyond what the headlines say. If
nothing particularly stands out, set notable to false.

Respond with this JSON schema only (JSON only, no explanatory prose):
{{"notable": true or false,
  "notes": "1-3 sentence summary in English (shown verbatim in a Telegram alert), or empty"}}
"""


def research_company(backend, company):
    """Returns: {"headlines": [...], "notable": bool, "notes": str}.

    Never raises, even on failure — this is supplementary information and must not block the
    main tailor pipeline (the resume rewrite). If any step fails it falls through quietly
    with notable=False.
    """
    headlines = _fetch_news_headlines(company)
    news_blob = "\n".join(f"- {h}" for h in headlines) if headlines else "(no news)"

    try:
        raw = backend.complete(
            RESEARCH_PROMPT_TEMPLATE.format(company=company, news_blob=news_blob)
        )
        parsed = _parse_json_response(raw)
    except Exception as e:
        print(f"  [company research] summary failed (skipping): {e}")
        parsed = {"notable": False, "notes": ""}

    return {
        "headlines": headlines,
        "notable": bool(parsed.get("notable")),
        "notes": (parsed.get("notes") or "").strip(),
    }
