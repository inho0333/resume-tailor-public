"""Trackly — an aggregator, driven by preferences you set on their side.

The recommended first source: one account covers every company in their index
instead of a board token per employer, and the search runs against your saved
preferences rather than a query you maintain here.

Three things about it cost real debugging time, so they are worth knowing before
you choose it:

1. **Coverage is set in the Trackly web app, not here.** Passing `--sponsorship`
   or a different region on the CLI does not widen what comes back; the saved
   server-side preference decides. Measured: `--sponsorship all` returned
   exactly the same set as the saved `needsSponsorship` preference. The CLI
   flags this source does pass (`--function`, `--limit`, `--sort`) narrow that
   set; they cannot widen it. So the honest instruction during setup is "set
   your preferences in the web app", and `check_auth` prints what the server
   currently thinks they are.

2. **A company absent from their index is invisible, permanently.** Trackly
   crawls per company. `trackly search "<name>"` returning an empty array means
   that employer will never appear, however well it matches — measured on a
   company whose board was perfectly reachable. `trackly request-company <name>
   --url <careers url>` asks them to add it; polling that company's own board
   through the greenhouse/lever/ashby sources is the fix that works today.

3. **The list view carries no posting body.** `trackly jobs` returns titles and
   URLs; only `trackly job <id>` has the description, and detail-fetching every
   posting on every poll is wasteful. So this source sets `needs_jd_lookup` and
   `jd_lookup` resolves the body from the posting URL — which usually works,
   because a good share of those URLs are Greenhouse or Lever.

Install: `npm install -g trackly-cli`. Authenticate with `trackly api-key create`
and `export TRACKLY_API_KEY=...` (preferred: no browser, works under a
scheduler), or `trackly login` for the Google OAuth flow.
"""

import json
import os
import shutil
import subprocess

from tracker import posting as posting_mod
from tracker.sources import base

# A launchd job's PATH is `/usr/bin:/bin:/usr/sbin:/sbin`, and Homebrew is not on
# it. Both the trackly binary AND the `#!/usr/bin/env node` shebang inside it
# resolve through PATH, so a bare subprocess.run(["trackly", ...]) works in an
# interactive shell and fails silently under the scheduler — which is the most
# confusing failure mode there is, because testing it by hand always passes.
# Hence an absolute binary and an explicit PATH on every call.
_EXTRA_PATH = "/opt/homebrew/bin:/usr/local/bin"


def _binary():
    return shutil.which("trackly") or "/opt/homebrew/bin/trackly"


def _env():
    return {**os.environ, "PATH": f"{_EXTRA_PATH}:{os.environ.get('PATH', '')}"}


def _run(args, timeout=60):
    """Run the CLI and parse its JSON. Raises RuntimeError with the reason."""
    cmd = [_binary(), *args, "--json"]
    key = os.environ.get("TRACKLY_API_KEY")
    if key:
        cmd += ["--api-key", key]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True,
                                timeout=timeout, env=_env())
    except FileNotFoundError:
        raise RuntimeError("the trackly CLI is not installed "
                           "(npm install -g trackly-cli)")
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"the trackly CLI did not finish within {timeout}s")
    if result.returncode != 0:
        raise RuntimeError(f"trackly exited {result.returncode}: {result.stderr[:300]}")
    try:
        return json.loads(result.stdout)
    except ValueError as e:
        # Their API occasionally answers with prose ("Received a non-JSON
        # response"), which lands on stdout and is not an exception by itself.
        raise RuntimeError(f"trackly's reply was not JSON: {e} / "
                           f"stdout[:200]={result.stdout[:200]!r}")


class Source(base.Source):
    name = "trackly"
    summary = ("One account, every company in their index, filtered by preferences you "
               "set on their site. The broadest single source — but a company they do "
               "not crawl is invisible, and the listing carries no posting body.")
    needs_jd_lookup = True

    def check_auth(self):
        if not shutil.which("trackly") and not os.path.exists("/opt/homebrew/bin/trackly"):
            return base.Status(False, "the trackly CLI is not installed",
                               fix="npm install -g trackly-cli")
        try:
            prefs = _run(["preferences"])
        except RuntimeError as e:
            return base.Status(
                False, str(e),
                fix=("trackly api-key create   # then: export TRACKLY_API_KEY=...\n"
                     "       trackly login            # or the browser flow"))
        saved = (prefs or {}).get("preferences") or {}
        # Printed because this is the thing people get wrong: these values are
        # what decides coverage, and nothing in this repo can change them.
        detail = ("your saved preferences (change them on the Trackly site, not here): "
                  f"functions={','.join(saved.get('wantedJobFunctions') or []) or '-'} · "
                  f"region={saved.get('locationFilter') or '-'} · "
                  f"type={saved.get('jobModality') or '-'} · "
                  f"sponsorship_needed={saved.get('needsSponsorship')}")
        return base.Status(True, detail)

    def fetch(self):
        args = ["jobs", "--sort", "newest", "--limit", str(self.settings.get("limit") or 50)]
        functions = self.settings.get("functions") or []
        if functions:
            # --function narrows the saved preference; it cannot widen it.
            args += ["--function", ",".join(functions)]
        try:
            jobs = _run(args)
        except RuntimeError as e:
            print(f"  [trackly] {e}")
            return []
        out = []
        for job in (jobs if isinstance(jobs, list) else []):
            facts = {}
            if job.get("location"):
                facts["location"] = job["location"]
            employment = base.normalise_employment(job.get("jobType"))
            if employment:
                facts["employment_type"] = employment
            out.append(posting_mod.Posting(
                id=str(job.get("id")),
                source=self.name,
                company=job.get("companyName") or "",
                title=job.get("title") or "",
                url=job.get("jobUrl") or "",
                updated_at=str(job.get("postedAt") or ""),
                text="",  # see the module docstring: the listing has no body
                facts=facts,
            ))
        return out
