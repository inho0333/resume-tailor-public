"""Greenhouse's public job board API. No key, no account, JSON, and the posting
body comes with the listing.

    https://boards-api.greenhouse.io/v1/boards/<board_token>/jobs?content=true

`content=true` is what makes this the best source in the project: the body
arrives with the list, so a posting is judged on its real text without a second
request and without any of the JavaScript-rendering trouble that makes scraping
a careers page unreliable.

Finding a company's board token: open their careers page and look for
`boards.greenhouse.io/<token>` or `job-boards.greenhouse.io/<token>` in the URL
or the embedded iframe. That token is the whole configuration.
"""

from tracker import posting as posting_mod
from tracker.sources import base, html_text

API_BASE = "https://boards-api.greenhouse.io/v1/boards"


class Source(base.Source):
    name = "greenhouse"
    summary = ("Companies you name, polled directly from their Greenhouse board. "
               "No account; the posting body comes with the listing, which makes it "
               "the most accurate source here.")

    def _tokens(self):
        """[(display name, board token)] from user_config.yaml."""
        out = []
        for entry in (self.settings.get("companies") or []):
            if isinstance(entry, str):
                out.append((entry, entry))
            elif isinstance(entry, dict) and entry.get("board_token"):
                out.append((entry.get("name") or entry["board_token"], entry["board_token"]))
        return out

    def check_auth(self):
        tokens = self._tokens()
        if not tokens:
            return base.Status(
                False,
                "no Greenhouse boards listed",
                fix=("add them under tracker.greenhouse.companies in user_config.yaml, "
                     "e.g.  - {name: Acme, board_token: acme}"))
        # One real request, because a placeholder token looks exactly like a
        # quiet board: 404 every two minutes for a week, reported as "nothing new".
        name, token = tokens[0]
        data = self._get_json(f"{API_BASE}/{token}/jobs")
        if data is None:
            return base.Status(False, f"board {token!r} ({name}) did not answer",
                               fix=f"check the token at https://boards.greenhouse.io/{token}")
        return base.Status(True, f"{len(tokens)} board(s), {token} answered with "
                                 f"{len(data.get('jobs') or [])} postings")

    def fetch(self):
        out = []
        for name, token in self._tokens():
            data = self._get_json(f"{API_BASE}/{token}/jobs", params={"content": "true"})
            if not data:
                continue
            for job in data.get("jobs") or []:
                offices = [o.get("name") for o in (job.get("offices") or []) if o.get("name")]
                out.append(posting_mod.Posting(
                    id=f"{token}:{job.get('id')}",
                    source=self.name,
                    company=name,
                    title=job.get("title") or "",
                    url=job.get("absolute_url") or "",
                    updated_at=job.get("updated_at") or "",
                    text=html_text.to_text(job.get("content") or ""),
                    facts={"location": ", ".join(offices[:2])} if offices else {},
                ))
        return out


def fetch_single(board_token, job_id):
    """One posting's body, for a URL another source handed over. "" on failure."""
    source = Source({})
    data = source._get_json(f"{API_BASE}/{board_token}/jobs/{job_id}",
                            params={"content": "true"})
    if not data:
        return ""
    return html_text.to_text(data.get("content") or "")
