"""Lever's public postings API. No key, no account, JSON, body included.

    https://api.lever.co/v0/postings/<client_name>?mode=json

The client name is the path segment on their careers page:
`jobs.lever.co/<client_name>`.
"""

from tracker import posting as posting_mod
from tracker.sources import base

API_BASE = "https://api.lever.co/v0/postings"


def _body(post):
    """Lever splits a posting across a description and a list of sections."""
    parts = [post.get("descriptionPlain") or post.get("description") or ""]
    for section in (post.get("lists") or []):
        parts.append(section.get("text") or "")
        parts.append(section.get("content") or "")
    from tracker.sources import html_text

    return html_text.to_text("\n".join(p for p in parts if p))


class Source(base.Source):
    name = "lever"
    summary = ("Companies you name, polled directly from their Lever board. "
               "No account; the posting body comes with the listing.")

    def _clients(self):
        out = []
        for entry in (self.settings.get("companies") or []):
            if isinstance(entry, str):
                out.append((entry, entry))
            elif isinstance(entry, dict) and entry.get("client_name"):
                out.append((entry.get("name") or entry["client_name"], entry["client_name"]))
        return out

    def check_auth(self):
        clients = self._clients()
        if not clients:
            return base.Status(
                False, "no Lever boards listed",
                fix=("add them under tracker.lever.companies in user_config.yaml, "
                     "e.g.  - {name: Acme, client_name: acme}"))
        name, client = clients[0]
        data = self._get_json(f"{API_BASE}/{client}", params={"mode": "json"})
        if data is None:
            return base.Status(False, f"board {client!r} ({name}) did not answer",
                               fix=f"check the name at https://jobs.lever.co/{client}")
        return base.Status(True, f"{len(clients)} board(s), {client} answered with "
                                 f"{len(data) if isinstance(data, list) else 0} postings")

    def fetch(self):
        out = []
        for name, client in self._clients():
            data = self._get_json(f"{API_BASE}/{client}", params={"mode": "json"})
            if not isinstance(data, list):
                continue
            for post in data:
                categories = post.get("categories") or {}
                facts = {}
                if categories.get("location"):
                    facts["location"] = categories["location"]
                employment = base.normalise_employment(categories.get("commitment"))
                if employment:
                    facts["employment_type"] = employment
                if str(categories.get("location") or "").lower().startswith("remote"):
                    facts["work_arrangement"] = "remote"
                out.append(posting_mod.Posting(
                    id=f"{client}:{post.get('id')}",
                    source=self.name,
                    company=name,
                    title=post.get("text") or "",
                    url=post.get("hostedUrl") or "",
                    updated_at=str(post.get("createdAt") or ""),
                    text=_body(post),
                    facts=facts,
                ))
        return out


def fetch_single(client_name, posting_id):
    """One posting's body, for a URL another source handed over. "" on failure."""
    source = Source({})
    data = source._get_json(f"{API_BASE}/{client_name}/{posting_id}",
                            params={"mode": "json"})
    return _body(data) if isinstance(data, dict) else ""
