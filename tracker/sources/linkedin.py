"""LinkedIn: the seam, and why there is no implementation behind it.

LinkedIn has the broadest index of any source here, and it is also the one this
project will not scrape. The reasons are specific, not squeamish:

- Reading your job feed programmatically requires driving a logged-in session.
  That is against LinkedIn's terms of use, and the account it risks is the one
  your job search runs through. Losing it costs more than any posting it finds.
- Their partner Job Search API is not open to individuals, so there is no
  sanctioned path for a tool like this one.
- A scraper in a public repository is also a maintenance promise. Selector-level
  breakage lands on whoever installed it, usually as an empty result that looks
  like a quiet week rather than a failure.

So this file exists to be the shape of a source, and to say all of that in one
place instead of in an issue thread.

## What to use instead

LinkedIn's own job alert emails carry the postings you would have scraped, and
reading your own mailbox is nobody's terms-of-service problem. There is no email
source in this repo yet; if you build one, it belongs beside this file and the
interface it has to satisfy is in `base.py`. Set up an alert for each search you
care about, then parse the digest.

In the meantime, almost every posting that reaches LinkedIn also sits on the
employer's own board, which the greenhouse, lever, ashby and apple sources read
directly — with the posting body attached, which LinkedIn's listing does not
give you anyway.

## If you implement it regardless

It is your account and your decision. `fetch()` has to return
`tracker.posting.Posting` objects, `check_auth()` has to say honestly whether a
session exists, and a browser dependency belongs in your fork rather than in
`requirements.txt` here. Keep it off by default.
"""

from tracker.sources import base


class Source(base.Source):
    name = "linkedin"
    implemented = False
    summary = ("Not implemented, on purpose: it needs a logged-in session, which is "
               "against their terms and risks the account your job search depends on. "
               "Use the company-board sources, or a LinkedIn job-alert email.")

    def check_auth(self):
        return base.Status(
            False,
            "not implemented — see the explanation in tracker/sources/linkedin.py",
            fix=("use greenhouse / lever / ashby / apple for the same companies, or set "
                 "up a LinkedIn job alert and read it from your mailbox"),
            unavailable=True)

    def fetch(self):
        """Always empty. A source that cannot deliver must not look like a quiet day —
        `watch` refuses to enable an unimplemented source, so this is never called."""
        return []
