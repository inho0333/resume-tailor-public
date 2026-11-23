"""One posting, in the one shape the rest of the pipeline reads.

Every source normalises to this. The fields a source cannot fill stay empty
rather than being guessed, because the whole pipeline downstream treats "unknown"
as "do not screen on it" — a guessed location would be screened on.
"""

import dataclasses
import re


@dataclasses.dataclass
class Posting:
    """What a source saw.

    id:      unique within the source. Used as the dedupe key, so it has to be
             the posting's own identifier and not, say, its position in a list.
    source:  which source saw it, for the dedupe bucket and the report.
    text:    the posting body, when the source hands it over. An aggregator
             usually does not (its list view has titles only), and `jd_lookup`
             fills it in later from the URL.
    """

    id: str
    source: str
    company: str = ""
    title: str = ""
    url: str = ""
    updated_at: str = ""
    text: str = ""
    # Facts the source published about the posting, same keys as tracker.jsonld
    # produces: location, work_arrangement, employment_type, comp_range,
    # years_required. They skip the model entirely.
    facts: dict = dataclasses.field(default_factory=dict)

    def __post_init__(self):
        self.id = str(self.id or "").strip()
        self.company = " ".join(str(self.company or "").split())
        self.title = " ".join(str(self.title or "").split())
        self.url = str(self.url or "").strip()
        self.text = str(self.text or "").strip()

    @property
    def label(self):
        return f"{self.company or '?'} · {self.title or '?'}"

    def fingerprint(self):
        """A key for "the same posting, seen through another source".

        Aggregators repost. Without this, a posting found on Trackly and again
        on the company's own board arrives twice, and the second copy is
        indistinguishable from a new opening.
        """
        return f"{_norm(self.company)}|{_norm(self.title)}"


def _norm(value):
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()
