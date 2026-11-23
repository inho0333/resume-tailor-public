"""Remotive's public API — the source that works with no account at all.

    https://remotive.com/api/remote-jobs?category=...&search=...&limit=N

It is the one source a brand-new setup can use in its first minute: no key, no
board token, no company list, and the posting body comes with the listing. That
is why `run.py setup` offers it to anyone who declines the others, and why the
cold-start check in the README uses it.

The trade is scope: everything here is a remote role, and the index is far
smaller than an aggregator's. Treat it as the thing that proves your pipeline
works end to end, and as a genuine source if remote is what you want anyway.

Remotive asks that their jobs be linked back to the Remotive URL and credited as
the source. This tool keeps a posting's own `url`, which is that link, and shows
it to one person — you — so both of those hold. Do not re-publish what comes out
of here.

## Why this is not The Muse

The Muse's public jobs API was the obvious candidate and is in several lists of
free job APIs. It now sits behind a bot challenge: every request from a
non-browser client, with or without a plausible User-Agent, returns 403 with a
Cloudflare interstitial. Verified directly before writing this. A source that
cannot answer is worse than no source, because its empty result reads as a quiet
day — so it is documented in ../CHANNELS.md and not implemented.
"""

from tracker import posting as posting_mod
from tracker.sources import base, html_text

API_URL = "https://remotive.com/api/remote-jobs"
CATEGORIES_URL = "https://remotive.com/api/remote-jobs/categories"

class Source(base.Source):
    name = "remotive"
    summary = ("Remote roles from a public index. No account, no keys, no company list — "
               "the one source that works the minute you install this. Remote only, and "
               "a smaller index than an aggregator's.")

    def _params(self):
        params = {"limit": int(self.settings.get("limit") or 50)}
        if self.settings.get("category"):
            params["category"] = self.settings["category"]
        if self.settings.get("search"):
            params["search"] = self.settings["search"]
        return params

    def categories(self):
        """Their category names, for `run.py setup` to offer.

        Printed rather than guessed: an unrecognised category returns an empty
        result set, not an error, so a typo looks exactly like a quiet index.
        """
        data = self._get_json(CATEGORIES_URL)
        return [c.get("name") for c in ((data or {}).get("jobs") or []) if c.get("name")]

    def check_auth(self):
        data = self._get_json(API_URL, params={"limit": 1})
        if data is None:
            return base.Status(False, "remotive.com did not answer",
                               fix="retry — this source needs no credentials, so a "
                                   "failure here is the network or their API")
        count = len(data.get("jobs") or [])
        if not count:
            return base.Status(
                False, "the query matched nothing",
                fix=("check tracker.remotive.category against the names their API uses; "
                     "`run.py setup` lists them"))
        return base.Status(True, f"ready, no credentials needed "
                                 f"({data.get('total-job-count', '?')} postings indexed)")

    def fetch(self):
        data = self._get_json(API_URL, params=self._params())
        out = []
        for job in ((data or {}).get("jobs") or []):
            facts = {
                # Every posting on Remotive is remote; that is the whole index.
                "work_arrangement": "remote",
            }
            if job.get("candidate_required_location"):
                facts["location"] = job["candidate_required_location"]
            employment = base.normalise_employment(job.get("job_type"))
            if employment:
                facts["employment_type"] = employment
            if job.get("salary"):
                # Free text ("$14/hour", "80k-100k"), not a parsed range — it is
                # shown to you and never compared against your floor, because a
                # mis-parsed number would silently drop real postings.
                facts["comp_stated"] = job["salary"]
            out.append(posting_mod.Posting(
                id=str(job.get("id")),
                source=self.name,
                company=job.get("company_name") or "",
                title=job.get("title") or "",
                url=job.get("url") or "",
                updated_at=str(job.get("publication_date") or ""),
                text=html_text.to_text(job.get("description") or ""),
                facts=facts,
            ))
        return out
