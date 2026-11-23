"""The tracker: the source contract, deduplication, and reading a page's markup.

Nothing here touches the network. Each source is driven against a recorded
response shape, which is the point of `_get_json` being the one place a source
makes a request.

The deduplication tests are the ones that matter most. Every case in them is a
duplicate that actually happened and cost a full drafting run, or a posting that
was silently burned and could never be reconsidered.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tracker
from tracker import dedupe, jd_lookup, jsonld
from tracker import posting as posting_mod
from tracker.sources import base


def _posting(**kwargs):
    defaults = {"id": "1", "source": "test", "company": "Acme",
                "title": "Product Manager", "url": "https://example.com/1"}
    defaults.update(kwargs)
    return posting_mod.Posting(**defaults)


# ── the source contract ─────────────────────────────────────────────────────

def test_every_registered_source_satisfies_the_contract(monkeypatch):
    """Driven offline: `_get_json` returning None is what an unreachable API
    looks like, and the subprocess a CLI-backed source would run is replaced by
    the error it raises when the CLI is absent. No test here reaches the
    network — a suite that does passes or fails on someone else's uptime."""
    from tracker.sources import trackly

    monkeypatch.setattr(base.Source, "_get_json", lambda *a, **k: None)
    monkeypatch.setattr(trackly, "_run",
                        lambda *a, **k: (_ for _ in ()).throw(
                            RuntimeError("the trackly CLI is not installed")))
    monkeypatch.setattr(trackly.shutil, "which", lambda _name: None)
    monkeypatch.setattr(trackly.os.path, "exists", lambda _path: False)

    for name in tracker.SOURCE_NAMES:
        source = tracker.build(name, {})
        assert source is not None, name
        assert source.name == name
        assert source.summary, f"{name} has no summary for `run.py setup`"
        status = source.check_auth()
        assert isinstance(status, base.Status), name
        assert status.ok is False, f"{name} reported ready with nothing configured"
        # An unconfigured source must say what to type, not merely that it is sad.
        assert status.fix or status.unavailable, name


def test_linkedin_is_an_interface_and_never_pollable():
    """Deliberately unimplemented: it needs a logged-in session, which is against
    their terms and risks the account the job search runs through. The file says
    so; this holds it shut."""
    source = tracker.build("linkedin", {})
    assert source.implemented is False
    status = source.check_auth()
    assert status.ok is False
    assert status.unavailable is True
    assert source.fetch() == []


def test_an_unconfigured_board_source_asks_for_its_identifier(monkeypatch):
    """A board source with no companies must fail its auth check rather than
    returning an empty list. An empty list is indistinguishable from a quiet
    day, and that is how one placeholder board token was polled every two
    minutes for a week and reported throughout as "no new postings"."""
    for name in ("greenhouse", "lever", "ashby", "apple"):
        source = tracker.build(name, {})
        monkeypatch.setattr(source, "_get_json", lambda *a, **k: None)
        status = source.check_auth()
        assert status.ok is False, name
        assert status.fix, name


def test_greenhouse_normalises_a_posting(monkeypatch):
    from tracker.sources import ats_greenhouse

    source = ats_greenhouse.Source({"companies": [{"name": "Acme", "board_token": "acme"}]})
    monkeypatch.setattr(source, "_get_json", lambda *a, **k: {"jobs": [{
        "id": 42, "title": "Product Manager, Payments",
        "absolute_url": "https://acme.example/jobs/42",
        "updated_at": "2026-11-01T00:00:00Z",
        "content": "&lt;p&gt;Own the roadmap.&lt;/p&gt;&lt;li&gt;5 years&lt;/li&gt;",
        "offices": [{"name": "London"}],
    }]})
    [post] = source.fetch()
    assert post.id == "acme:42"
    assert post.source == "greenhouse"
    assert post.facts["location"] == "London"
    # The body arrives double-escaped from some endpoints: unescape has to run
    # before tags are stripped, or the markup ends up in the text a model reads.
    assert "<p>" not in post.text
    assert "Own the roadmap." in post.text


def test_ashby_does_not_claim_every_posting_is_remote(monkeypatch):
    """Measured on a real board: every posting reported isRemote true, including
    ones located at the company's headquarters. Mapping that onto
    work_arrangement marks everything remote, which switches the location check
    off for all of them and tells a remote-only searcher that an on-site role
    qualifies."""
    from tracker.sources import ats_ashby

    source = ats_ashby.Source({"companies": [{"name": "Acme", "org": "acme"}]})
    monkeypatch.setattr(source, "_get_json", lambda *a, **k: {"jobs": [{
        "id": "abc", "title": "Engineer", "location": "New York, NY (HQ)",
        "isRemote": True, "employmentType": "FullTime",
        "jobUrl": "https://jobs.ashbyhq.com/acme/abc",
        "descriptionPlain": "x" * 300,
    }]})
    [post] = source.fetch()
    assert "work_arrangement" not in post.facts
    assert post.facts["employment_type"] == "full_time"


def test_remotive_keeps_the_pay_string_the_source_published(monkeypatch):
    """A model handed the same posting reported "0.2333 USD" — a number it had
    derived. A string the source published is the posting's own claim."""
    from tracker.sources import remotive

    source = remotive.Source({})
    monkeypatch.setattr(source, "_get_json", lambda *a, **k: {"jobs": [{
        "id": 7, "title": "Content Reviewer", "company_name": "Telus",
        "url": "https://remotive.com/x", "publication_date": "2026-11-19T00:00:00",
        "job_type": "part_time", "candidate_required_location": "USA",
        "salary": "$14/hour", "description": "<p>Review things.</p>",
    }]})
    [post] = source.fetch()
    assert post.facts["comp_stated"] == "$14/hour"
    assert post.facts["employment_type"] == "part_time"
    # Every posting on that index is remote; that is the whole index.
    assert post.facts["work_arrangement"] == "remote"


# ── deduplication ───────────────────────────────────────────────────────────

def test_a_posting_is_new_once():
    state = {}
    post = _posting()
    assert dedupe.is_new(state, "test", post.id) is True
    dedupe.mark(state, post)
    assert dedupe.is_new(state, "test", post.id) is False


def test_the_same_opening_through_two_sources_is_one_posting():
    """Measured: one posting arrived as a company's own Greenhouse id and again
    as an aggregator's id, and ran the whole drafting pipeline twice."""
    state = {}
    dedupe.mark(state, _posting(id="gh-1", source="greenhouse"))
    later = _posting(id="agg-999", source="trackly")
    assert dedupe.is_new(state, "trackly", later.id) is True
    assert dedupe.is_duplicate(state, later) is True


def test_a_legal_suffix_does_not_make_a_new_company():
    """"Rocket Lab" and "Rocket Lab Corporation" are one employer."""
    state = {}
    dedupe.mark(state, _posting(company="Rocket Lab", title="Systems Engineer"))
    assert dedupe.is_duplicate(
        state, _posting(id="2", company="Rocket Lab Corporation",
                        title="Systems Engineer")) is True


def test_a_generic_title_does_not_collapse_across_companies():
    """The other half of that rule. "Product Management Intern" is posted by
    hundreds of unrelated employers, and merging them costs a missed opening —
    which is the expensive mistake."""
    state = {}
    dedupe.mark(state, _posting(company="Acme", title="Product Management Intern"))
    assert dedupe.is_duplicate(
        state, _posting(id="2", company="Globex",
                        title="Product Management Intern")) is False


def test_the_cross_source_window_expires():
    """The same title reopening next season is a new opening, so the window is
    deliberately finite."""
    import datetime

    state = {}
    post = _posting()
    dedupe.mark(state, post)
    old = (datetime.datetime.now() - datetime.timedelta(days=90)).isoformat()
    state["_fingerprints"][post.fingerprint()]["first_seen"] = old
    assert dedupe.is_duplicate(state, post) is False


def test_an_unreadable_timestamp_counts_as_unseen():
    state = {}
    post = _posting()
    dedupe.mark(state, post)
    state["_fingerprints"][post.fingerprint()]["first_seen"] = "not a date"
    assert dedupe.is_duplicate(state, post) is False


def test_the_first_poll_of_a_source_sends_nothing():
    """Without baselining, turning on a source fires one notification per posting
    in its whole index — measured at around fifty."""
    state = {}
    postings = [_posting(id=str(i), title=f"Role {i}") for i in range(50)]
    dedupe.baseline(state, postings)
    assert dedupe.count(state, "test") == 50
    assert all(not dedupe.is_new(state, "test", p.id) for p in postings)


def test_a_filtered_posting_is_kept_apart_from_a_seen_one():
    """The pipeline this came from marked a rejected posting seen, which burned
    it: improving the filter changed nothing for anything already dropped, and
    nothing distinguished "you were asked about this" from "something decided
    not to ask you"."""
    state = {}
    post = _posting()
    dedupe.mark_filtered(state, post, "different line of work")
    assert dedupe.was_filtered(state, post) is True
    # not marked seen, so clearing the rejections is enough to reconsider it
    assert dedupe.is_new(state, "test", post.id) is True
    dedupe.clear_filtered(state)
    assert dedupe.was_filtered(state, post) is False


def test_prune_keeps_what_it_cannot_read():
    """Dropping an unparseable entry would make that posting new again and
    re-notify it, so the safe direction is to keep it."""
    import datetime

    state = {"test": {
        "fresh": {"first_seen": datetime.datetime.now().isoformat()},
        "ancient": {"first_seen": (datetime.datetime.now()
                                   - datetime.timedelta(days=400)).isoformat()},
        "broken": {"title": "no timestamp at all"},
    }}
    pruned = dedupe.prune(state, days=180)
    assert set(pruned["test"]) == {"fresh", "broken"}


def test_state_survives_an_unreadable_file(tmp_path):
    """A corrupted state file costs one round of duplicate notifications, which
    is recoverable. Refusing to run is not."""
    path = tmp_path / "seen.json"
    path.write_text("{not json")
    assert dedupe.load(str(path)) == {}


def test_state_is_written_atomically(tmp_path):
    """A half-written file reads as "nothing has ever been seen", and the next
    tick re-notifies every posting in every source."""
    path = tmp_path / "state" / "seen.json"
    state = {}
    dedupe.mark(state, _posting())
    dedupe.save(state, str(path))
    assert dedupe.load(str(path)) == state
    assert not list(path.parent.glob("*.tmp"))


# ── reading a page's own markup ──────────────────────────────────────────────

_LD_JSON = """
<script type="application/ld+json">
{"@type":"JobPosting","title":"Product Manager","description":"<p>Own&nbsp;it.</p>",
 "hiringOrganization":{"@type":"Organization","name":"Acme"},
 "employmentType":"FULL_TIME","jobLocationType":"TELECOMMUTE",
 "jobLocation":{"@type":"Place","address":{"addressLocality":"San Francisco",
   "addressRegion":"CA","addressCountry":"US"}},
 "baseSalary":{"@type":"MonetaryAmount","currency":"USD",
   "value":{"minValue":180000,"maxValue":220000,"unitText":"YEAR"}},
 "experienceRequirements":{"monthsOfExperience":60},
 "validThrough":"2027-01-01"}
</script>
"""


def test_a_page_that_publishes_its_facts_needs_no_model():
    facts = jsonld.extract(_LD_JSON)
    assert facts["company"] == "Acme"
    assert facts["location"] == "San Francisco, CA, US"
    assert facts["work_arrangement"] == "remote"
    assert facts["employment_type"] == "full_time"
    assert facts["comp_range"] == {"min": 180000.0, "max": 220000.0,
                                   "currency": "USD", "period": "year"}
    assert facts["years_required"] == 5.0
    assert "Own it." in facts["text"]


def test_a_page_with_no_jobposting_block_claims_nothing():
    assert jsonld.extract("<html><body>nothing here</body></html>") == {}
    assert jsonld.extract("") == {}
    assert jsonld.extract('<script type="application/ld+json">{broken</script>') == {}


def test_the_longest_description_wins():
    """Several blocks can describe one posting — a summary card plus the posting
    itself. The long one is the real body."""
    page = ('<script type="application/ld+json">{"@type":"JobPosting",'
            '"description":"short"}</script>'
            '<script type="application/ld+json">{"@type":"JobPosting",'
            f'"description":"{"long " * 50}"}}</script>')
    assert len(jsonld.extract(page)["text"]) > 100


@pytest.mark.parametrize("graph", [
    '{"@graph":[{"@type":"JobPosting","description":"%s"}]}',
    '[{"@type":"WebPage"},{"@type":"JobPosting","description":"%s"}]',
    '{"@type":["JobPosting","Thing"],"description":"%s"}',
])
def test_every_ld_json_arrangement_is_flattened(graph):
    body = "x" * 400
    page = f'<script type="application/ld+json">{graph % body}</script>'
    assert jsonld.extract(page)["text"] == body


def test_an_application_form_schema_is_not_a_posting():
    """Measured: one JavaScript-rendered page produced 3,956 hits on these tokens
    where real postings produce zero. Without the check that JSON became the
    posting — its top ATS keywords were "label" and "choices", and the degree
    screen reported "unclear" because the requirements were never in the text."""
    form = " ".join(['{"questionId": 1, "choices": [], "isRequired": true}'] * 40)
    assert jd_lookup.looks_like_form_schema(form) is True
    assert jd_lookup.looks_like_form_schema("We are hiring a product manager. "
                                            "You will own the roadmap.") is False


def test_a_posting_that_already_has_a_body_is_not_refetched(monkeypatch):
    def _explode(_url):
        raise AssertionError("jd_lookup must not fetch a posting it already has")

    monkeypatch.setattr(jd_lookup, "fetch", _explode)
    post = _posting(text="y" * (jd_lookup.MIN_USEFUL_LENGTH + 1))
    assert jd_lookup.fill(post) is post


def test_a_source_fact_beats_a_page_fact(monkeypatch):
    """A source knows its own listing better than a page's markup does."""
    monkeypatch.setattr(jd_lookup, "fetch", lambda _url: {
        "text": "z" * 500, "facts": {"location": "from the page",
                                     "employment_type": "contract"}, "via": "jsonld"})
    post = _posting(facts={"location": "from the source"})
    filled = jd_lookup.fill(post)
    assert filled.facts["location"] == "from the source"
    assert filled.facts["employment_type"] == "contract"
    assert filled.facts["jd_via"] == "jsonld"
    assert len(filled.text) == 500
