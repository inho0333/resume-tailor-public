# Where postings come from

Pick one to start. Adding more later costs nothing, and the deduplication is
cross-source, so the same opening found twice is only ever asked about once.

`python run.py setup` walks this list and checks each choice against its API
before writing it down. `python run.py watch --source <name>` tries one.

| | Source | Account | Coverage | Posting body |
|---|---|---|---|---|
| 1 | **trackly** | free account | every company in their index | no — fetched from the link |
| 2 | **greenhouse** | none | the companies you name | yes |
| 2 | **lever** | none | the companies you name | yes |
| 2 | **ashby** | none | the companies you name | yes |
| 2 | **apple** | none | Apple | yes, on demand |
| 3 | **remotive** | none | remote roles, small index | yes |
| — | **linkedin** | — | not implemented, deliberately | — |

The numbers are a suggested order of adoption, not a ranking. Which is actually
best depends on whether you are searching broadly or already know the twenty
employers you care about.

---

## 1. Trackly — start here if you are searching broadly

One account covers every company Trackly indexes, and the search runs against
preferences you save on their site rather than a query you maintain here.

```
npm install -g trackly-cli
trackly api-key create          # then: export TRACKLY_API_KEY=...
```

`trackly login` works too (Google, in a browser). Prefer the API key: it needs
no browser, which matters when `watch` runs under a scheduler.

```yaml
tracker:
  sources: [trackly]
  trackly:
    limit: 50
    functions: [product, strategy]   # narrows; see below
```

**Three things to know before you rely on it.**

- **Coverage is set on their site, not here.** Your saved preferences — function,
  region, employment type, sponsorship — decide what the API will return at all.
  Passing different values on the CLI does not widen that: measured,
  `--sponsorship all` returned exactly the set the saved preference allowed. The
  `functions` setting above narrows the saved set; it cannot widen it. So set
  your preferences in the Trackly web app first. `run.py doctor` prints what the
  server currently thinks they are, so the two never drift silently.
- **A company they do not crawl is invisible, permanently.** Trackly crawls per
  company. If `trackly search "<name>"` comes back empty, that employer will
  never appear however well it matches — measured on a company whose own job
  board was perfectly reachable the whole time. `trackly request-company <name>
  --url <careers url>` asks them to add it; watching that company's own board
  through greenhouse/lever/ashby is the fix that works today.
- **The listing has no posting body.** Only their single-posting endpoint does,
  and fetching every posting on every poll is wasteful. So the body is resolved
  from the posting's URL instead (`tracker/jd_lookup.py`), which usually works
  because a good share of those URLs are Greenhouse or Lever.

## 2. The company's own board — the most accurate thing here

If you already know which employers you want, this beats any aggregator: public
API, no account, no rate limit worth worrying about, and the full posting body
arrives with the listing.

```yaml
tracker:
  sources: [greenhouse, lever, ashby, apple]
  greenhouse:
    companies:
      - {name: Epic Games, board_token: epicgames}
  lever:
    companies:
      - {name: Palantir, client_name: palantir}
  ashby:
    companies:
      - {name: Ramp, org: ramp}
  apple:
    teams: [internships-STDNT-INTRN]
    location: united-states-USA
    include_keywords: [Business, Strategy, Operations]
    exclude_keywords: [Hardware, Software, PhD]
```

**Finding the identifier.** Open the company's careers page and read the URL:

- `boards.greenhouse.io/<board_token>` or `job-boards.greenhouse.io/<board_token>`
  (sometimes only visible in the embedded iframe's source)
- `jobs.lever.co/<client_name>`
- `jobs.ashbyhq.com/<org>`
- Apple: filter on jobs.apple.com and read the team id out of the URL

`run.py setup` and `run.py doctor` make one real request against the first entry
and tell you if it 404s. That check exists because of what its absence looked
like: a board token that was a placeholder from the start, polled every two
minutes for a week, 404 every time, reported as "no new postings".

Apple is the only source with an exclude list. The shared title filter is
include-only, and on a team that posts hundreds of unrelated roles the filter
you actually need is "business or strategy, but not hardware, software or PhD".

## 3. Remotive — the one that works immediately

No account, no keys, no company list. Remote roles only, and a much smaller
index than an aggregator's, so it is the source that proves your pipeline works
end to end and a real source if remote is what you want anyway.

```yaml
tracker:
  sources: [remotive]
  remotive:
    category: Product Management   # run.py setup lists the names their API accepts
    limit: 50
```

Remotive ask that their postings link back to Remotive and credit them as the
source. A posting's link is that URL, and it is shown to one person — you. Do
not re-publish what comes out of it.

### Why not The Muse

The Muse's public jobs API is in most lists of free job APIs and was the obvious
choice here. It now sits behind a bot challenge: every request from a
non-browser client returns 403 with a Cloudflare interstitial, with or without a
plausible User-Agent. A source that cannot answer is worse than no source,
because an empty result reads as a quiet day — so it is named here and not
implemented.

## LinkedIn — not implemented, on purpose

The broadest index of all, and the one this project will not scrape:

- Reading your feed programmatically means driving a logged-in session, which is
  against LinkedIn's terms and risks the account your job search runs through.
- Their Job Search API is not open to individuals, so there is no sanctioned path.
- A scraper in a public repo is a maintenance promise, and selector breakage
  lands on whoever installed it — usually as an empty result that looks like a
  quiet week.

`tracker/sources/linkedin.py` is the seam, kept so the reason lives in one place
and so a fork has something to fill in.

**What to use instead.** LinkedIn's job alert emails carry the same postings, and
reading your own mailbox is nobody's terms-of-service problem. There is no email
source here yet; if you write one it belongs next to that file. And almost
everything that reaches LinkedIn also sits on the employer's own board, which
the sources above read directly — with the posting body attached, which
LinkedIn's listing does not give you anyway.

## Adding a source

One file in `tracker/sources/` and one line in `tracker/__init__.py`. The
contract is three methods — `check_auth`, `fetch`, `describe` — and
`tracker/sources/remotive.py` is the shortest complete example. See
`docs/tracker.md`.
