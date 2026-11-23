"""What a source has to provide, and the HTTP manners they all share.

Three methods:

    check_auth()  →  Status   can this run right now, and if not, what exactly
                              do I type to fix it?
    fetch()       →  [Posting] everything it can see now. Not "everything new" —
                              deduplication is shared and lives in dedupe.py, so
                              a source never has to keep state.
    describe()    →  str       one line for `run.py setup` and `doctor`.

`check_auth` exists because of what the alternative looked like: a misconfigured
source returning an empty list is indistinguishable from a quiet day, and the
one real instance of that ran for a week — a board token that had been a
placeholder the whole time, polled every two minutes, 404 every time, and
reported as "no new postings".
"""

import dataclasses
import re


# Every source spells employment type its own way — "FullTime", "Full-time",
# "full_time", "Intern". The fit gate compares it against the search kind in
# profile.yaml, so it has to arrive in one vocabulary; anything unrecognised
# becomes None, and the type axis is then simply not screened.
_EMPLOYMENT_WORDS = {
    "fulltime": "full_time", "full": "full_time", "permanent": "full_time",
    "parttime": "part_time", "part": "part_time",
    "contract": "contract", "contractor": "contract", "freelance": "contract",
    "temporary": "contract", "temp": "contract",
    "intern": "internship", "internship": "internship", "placement": "internship",
    "newgrad": "new_grad", "graduate": "new_grad",
}


def normalise_employment(value):
    """'FullTime' / 'Full-time' / 'Intern' -> 'full_time' / 'internship'. None if unknown."""
    key = re.sub(r"[^a-z]", "", str(value or "").lower())
    if not key:
        return None
    if key in _EMPLOYMENT_WORDS:
        return _EMPLOYMENT_WORDS[key]
    # "fulltimeregular", "summerinternship" and similar compounds.
    for word, mapped in _EMPLOYMENT_WORDS.items():
        if word in key:
            return mapped
    return None


@dataclasses.dataclass
class Status:
    """Whether a source can run, and the exact command that fixes it if not."""

    ok: bool
    detail: str = ""
    # A command the user can paste. Not prose about what they ought to do.
    fix: str = ""
    # True when nothing the user types can enable it (linkedin).
    unavailable: bool = False


class Source:
    """A place postings come from."""

    name = "source"
    # What `run.py setup` tells the user about this source before they pick it.
    summary = ""
    # False when this source has no implementation and never returns postings.
    implemented = True
    # True when the source's listing carries no posting body, so `jd_lookup`
    # has to fetch each one from its URL before the pipeline can judge it.
    needs_jd_lookup = False

    def __init__(self, settings=None):
        self.settings = dict(settings or {})

    # ── contract ────────────────────────────────────────────────────────────

    def check_auth(self):
        raise NotImplementedError

    def fetch(self):
        raise NotImplementedError

    def describe(self):
        return self.summary

    # ── shared helpers ──────────────────────────────────────────────────────

    def _headers(self):
        """Every request carries a real User-Agent.

        A bare urllib or requests default gets 406 and 403 responses from job
        boards that serve the same URL happily to a browser — measured on more
        than one host, and it looks exactly like an outage.
        """
        import config

        return {"User-Agent": config.HTTP_USER_AGENT,
                "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9"}

    def _timeout(self):
        import config

        return config.HTTP_TIMEOUT_SECONDS

    def _get_json(self, url, params=None):
        """GET and parse JSON. None on any failure, with the reason printed.

        A source that cannot reach its API returns no postings; it does not
        raise. One unreachable board must not stop the other sources in the
        same tick from being polled.
        """
        import requests

        try:
            resp = requests.get(url, params=params, headers=self._headers(),
                                timeout=self._timeout())
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as e:
            print(f"  [{self.name}] request failed: {e}")
            return None
        except ValueError as e:
            print(f"  [{self.name}] response was not JSON: {e}")
            return None
