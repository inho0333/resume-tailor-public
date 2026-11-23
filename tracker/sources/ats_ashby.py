"""Ashby's public job board API. No key, no account, body included.

    https://api.ashbyhq.com/posting-api/job-board/<org>

Ashby careers pages render with JavaScript, so scraping one yields nothing and
this API is the only static way in — measured: a page that produced 0 characters
of body text through generic extraction returns 3,066 through here.

There is no public single-posting endpoint, so one posting is found by fetching
the board and picking it out by id. Boards on Ashby are usually small (tens of
postings), so that is cheap.
"""

from tracker import posting as posting_mod
from tracker.sources import base

API_BASE = "https://api.ashbyhq.com/posting-api/job-board"


class Source(base.Source):
    name = "ashby"
    summary = ("Companies you name, polled from their Ashby board. No account. "
               "Ashby pages are JavaScript-rendered, so this API is the only way to "
               "read them without a browser.")

    def _orgs(self):
        out = []
        for entry in (self.settings.get("companies") or []):
            if isinstance(entry, str):
                out.append((entry, entry))
            elif isinstance(entry, dict) and entry.get("org"):
                out.append((entry.get("name") or entry["org"], entry["org"]))
        return out

    def check_auth(self):
        orgs = self._orgs()
        if not orgs:
            return base.Status(
                False, "no Ashby boards listed",
                fix=("add them under tracker.ashby.companies in user_config.yaml, "
                     "e.g.  - {name: Acme, org: acme}"))
        name, org = orgs[0]
        data = self._get_json(f"{API_BASE}/{org}")
        if data is None:
            return base.Status(False, f"board {org!r} ({name}) did not answer",
                               fix=f"check the org at https://jobs.ashbyhq.com/{org}")
        return base.Status(True, f"{len(orgs)} board(s), {org} answered with "
                                 f"{len(data.get('jobs') or [])} postings")

    def fetch(self):
        out = []
        for name, org in self._orgs():
            data = self._get_json(f"{API_BASE}/{org}")
            if not data:
                continue
            for job in data.get("jobs") or []:
                facts = {}
                if job.get("location"):
                    facts["location"] = job["location"]
                employment = base.normalise_employment(job.get("employmentType"))
                if employment:
                    facts["employment_type"] = employment
                # `isRemote` is NOT mapped to work_arrangement. Measured on a real
                # board: every posting on it reported isRemote true, including ones
                # whose location was the company's headquarters. Feeding that in
                # would mark every role remote, which switches off the location
                # check for all of them and tells anyone searching remote-only
                # that an on-site role qualifies.
                out.append(posting_mod.Posting(
                    id=f"{org}:{job.get('id')}",
                    source=self.name,
                    company=job.get("companyName") or name,
                    title=job.get("title") or "",
                    url=job.get("jobUrl") or "",
                    updated_at=str(job.get("publishedAt") or ""),
                    text=(job.get("descriptionPlain") or "").strip(),
                    facts=facts,
                ))
        return out


def fetch_single(org, posting_id):
    """One posting's body. "" when the board or the id is not found."""
    source = Source({})
    data = source._get_json(f"{API_BASE}/{org}")
    for job in ((data or {}).get("jobs") or []):
        if str(job.get("id")) == str(posting_id):
            return (job.get("descriptionPlain") or "").strip()
    return ""
