"""Apple's own careers search endpoint.

Apple does not use a third-party ATS, and its postings are a large enough share
of some searches to be worth a source of their own. The search endpoint returns
JSON over a POST; the body of a posting needs a second request per posting,
which is why `needs_detail` exists — in the steady state only genuinely new
postings cost one.

This is the one source with an exclude list. The shared title filter is
include-only, and on a team like `internships-STDNT-INTRN` that is not enough:
the same team posts hundreds of roles and the useful filter is "business or
strategy, but not hardware, software or PhD".
"""

import json

import requests

from tracker import posting as posting_mod
from tracker.sources import base, html_text

SEARCH_URL = "https://jobs.apple.com/api/role/search"
DETAIL_URL = "https://jobs.apple.com/api/role"
PAGE_URL = "https://jobs.apple.com/en-us/details"


class Source(base.Source):
    name = "apple"
    summary = ("Apple's careers search. No account. The only source with an exclude "
               "list, because one Apple team posts hundreds of unrelated roles.")

    def _teams(self):
        return [t for t in (self.settings.get("teams") or []) if t]

    def _includes(self):
        return [k.lower() for k in (self.settings.get("include_keywords") or []) if k]

    def _excludes(self):
        return [k.lower() for k in (self.settings.get("exclude_keywords") or []) if k]

    def _matches(self, title):
        low = (title or "").lower()
        if any(k in low for k in self._excludes()):
            return False
        includes = self._includes()
        return not includes or any(k in low for k in includes)

    def _search(self, team, page=1):
        payload = {"query": "", "page": page, "locale": "en-us", "sort": "newest",
                   "filters": {"postingpostLocation": [self.settings.get("location")
                                                       or "united-states-USA"],
                               "teams": [{"team": team.split("-")[0], "subTeam": team}]}}
        try:
            resp = requests.post(SEARCH_URL, headers={**self._headers(),
                                                      "Content-Type": "application/json"},
                                 data=json.dumps(payload), timeout=self._timeout())
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException, ValueError) as e:
            print(f"  [apple] search failed for {team}: {e}")
            return None

    def check_auth(self):
        teams = self._teams()
        if not teams:
            return base.Status(
                False, "no Apple teams listed",
                fix=("add them under tracker.apple.teams in user_config.yaml. The team id "
                     "is in the URL when you filter on jobs.apple.com, "
                     "e.g. internships-STDNT-INTRN"))
        data = self._search(teams[0])
        if data is None:
            return base.Status(False, "jobs.apple.com did not answer",
                               fix="check the team id on jobs.apple.com")
        return base.Status(True, f"{len(teams)} team(s), "
                                 f"{len(data.get('searchResults') or [])} postings on page 1")

    def _detail_text(self, posting_id):
        try:
            resp = requests.get(f"{DETAIL_URL}/{posting_id}", headers=self._headers(),
                                timeout=self._timeout())
            resp.raise_for_status()
            data = resp.json()
        except (requests.RequestException, ValueError) as e:
            print(f"  [apple] detail failed for {posting_id}: {e}")
            return ""
        parts = [data.get("jobSummary"), data.get("description"),
                 data.get("minimumQualifications"), data.get("preferredQualifications")]
        return html_text.to_text("\n\n".join(p for p in parts if p))

    def fetch(self, needs_detail=None):
        """needs_detail(posting_id) -> bool. `watch` passes the dedupe check, so
        only postings that are actually new cost a second request."""
        out = []
        for team in self._teams():
            data = self._search(team)
            if not data:
                continue
            for job in data.get("searchResults") or []:
                title = job.get("postingTitle") or ""
                if not self._matches(title):
                    continue
                posting_id = str(job.get("positionId") or job.get("id") or "")
                locations = [l.get("name") for l in (job.get("locations") or [])
                             if l.get("name")]
                text = ""
                if needs_detail is None or needs_detail(f"apple:{posting_id}"):
                    text = self._detail_text(posting_id)
                out.append(posting_mod.Posting(
                    id=f"apple:{posting_id}",
                    source=self.name,
                    company="Apple",
                    title=title,
                    url=f"{PAGE_URL}/{posting_id}",
                    updated_at=str(job.get("postDateInGMT") or ""),
                    text=text,
                    facts={"location": ", ".join(locations[:2])} if locations else {},
                ))
        return out
