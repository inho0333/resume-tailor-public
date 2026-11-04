"""confirm_state — TTL expiry, duplicate suppression, concurrent clicks, blob storage.

The first tests in this repo. The TTL path had **never once run** in production (zero
expiries) at the time they were written, and a batch of nine records was about to expire at
once, so the behaviour was pinned before that happened rather than after.
"""

import datetime
import os

import pytest

import config
import confirm_state as cs


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(cs, "_STATE_FILE", str(tmp_path / "pending.json"))
    yield


def _aged(days):
    return (datetime.datetime.now() - datetime.timedelta(days=days)).isoformat()


# ── TTL ───────────────────────────────────────────────────────────────────────


def test_fresh_record_is_returned():
    sid = cs.register({"company": "Acme", "role": "PM"})
    assert cs.get(sid) is not None


def test_expired_record_reads_as_missing():
    sid = cs.register({"company": "Acme", "role": "PM"})
    state = cs._load()
    state[sid]["created_at"] = _aged(config.CONFIRM_TTL_DAYS + 1)
    cs._atomic_write(cs._STATE_FILE, state)
    assert cs.get(sid) is None


def test_prune_removes_only_expired():
    old = cs.register({"company": "Old", "role": "PM"})
    new = cs.register({"company": "New", "role": "PM"})
    state = cs._load()
    state[old]["created_at"] = _aged(config.CONFIRM_TTL_DAYS + 1)
    cs._atomic_write(cs._STATE_FILE, state)

    # prune_expired returns **the whole record**, not a (company, role) tuple: the caller
    # needs jd_profile/eligibility to book the expiry into the decision journal as an
    # implicit "no".
    dropped = cs.prune_expired()
    assert [r["company"] for r in dropped] == ["Old"]
    assert cs.get(new) is not None


def test_prune_handles_many_expiries_at_once():
    """Nine records expired in the same hour once — clearing several at a time must work."""
    ids = [cs.register({"company": f"C{i}", "role": "PM"}) for i in range(9)]
    state = cs._load()
    for sid in ids:
        state[sid]["created_at"] = _aged(config.CONFIRM_TTL_DAYS + 2)
    cs._atomic_write(cs._STATE_FILE, state)

    assert len(cs.prune_expired()) == 9
    assert cs._load() == {}


def test_record_without_created_at_is_stamped_not_deleted():
    """Deleting a record whose age is unknown would throw away a request the user is still
    waiting on, so it gets stamped instead."""
    sid = cs.register({"company": "Acme", "role": "PM"})
    state = cs._load()
    del state[sid]["created_at"]
    cs._atomic_write(cs._STATE_FILE, state)

    assert cs.prune_expired() == []
    assert cs._load()[sid]["created_at"]


# ── duplicate suppression ─────────────────────────────────────────────────────


@pytest.mark.parametrize("a,b", [
    ("Northwind Aerospace", "Northwind Aerospace Corporation"),
    ("Acme", "Acme Inc"),
    ("Acme", "ACME,  LLC"),
])
def test_company_suffix_variants_are_one_pending(a, b):
    role = "Government Operations Intern"
    assert cs.register({"company": a, "role": role}) == cs.register({"company": b, "role": role})


def test_apostrophe_variants_are_one_pending():
    assert cs.register({"company": "Dryft", "role": "Founder's Associate Intern"}) == \
           cs.register({"company": "Dryft", "role": "Founders Associate Intern"})


def test_same_url_is_one_pending_even_if_names_differ():
    """The scraper and the model often disagree on a company's name. Same URL, same posting."""
    assert cs.register({"company": "Nova", "role": "Biz Ops",
                        "url": "https://example.com/j/1"}) == \
           cs.register({"company": "NovaAI", "role": "Business Ops",
                        "url": "https://example.com/j/1/"})


def test_different_postings_stay_separate():
    a = cs.register({"company": "Acme", "role": "PM"})
    b = cs.register({"company": "Globex", "role": "PM"})
    assert a != b


def test_expired_duplicate_does_not_block_a_new_one():
    first = cs.register({"company": "Acme", "role": "PM"})
    state = cs._load()
    state[first]["created_at"] = _aged(config.CONFIRM_TTL_DAYS + 1)
    cs._atomic_write(cs._STATE_FILE, state)
    assert cs.register({"company": "Acme", "role": "PM"}) != first


# ── concurrent clicks ─────────────────────────────────────────────────────────


def test_only_the_first_click_claims():
    sid = cs.register({"company": "Acme", "role": "PM"})
    assert cs.try_claim(sid) is True
    assert cs.try_claim(sid) is False


def test_claim_keeps_the_record_for_retry():
    sid = cs.register({"company": "Acme", "role": "PM"})
    cs.try_claim(sid)
    assert cs.get(sid) is not None


def test_release_allows_a_retry():
    sid = cs.register({"company": "Acme", "role": "PM"})
    cs.try_claim(sid)
    cs.release_claim(sid)
    assert cs.try_claim(sid) is True


def test_claiming_a_missing_record_is_false():
    assert cs.try_claim("nope") is False


# ── the posting body is stored outside the index ──────────────────────────────


def test_large_jd_text_moves_out_of_the_index():
    big = "posting body " * 3000
    sid = cs.register({"company": "Acme", "role": "PM", "jd_text": big})
    assert os.path.getsize(cs._STATE_FILE) < 2000      # the index stays small
    assert cs.get(sid)["jd_text"] == big               # and reads back whole


def test_small_jd_text_stays_inline():
    sid = cs.register({"company": "Acme", "role": "PM", "jd_text": "short"})
    assert cs.get(sid)["jd_text"] == "short"


def test_discard_removes_the_blob():
    sid = cs.register({"company": "Acme", "role": "PM", "jd_text": "x" * 5000})
    path = cs._blob_path(sid)
    assert os.path.exists(path)
    cs.discard(sid)
    assert not os.path.exists(path)


def test_prune_removes_the_blob_too():
    sid = cs.register({"company": "Acme", "role": "PM", "jd_text": "x" * 5000})
    state = cs._load()
    state[sid]["created_at"] = _aged(config.CONFIRM_TTL_DAYS + 1)
    cs._atomic_write(cs._STATE_FILE, state)
    cs.prune_expired()
    assert not os.path.exists(cs._blob_path(sid))


def test_missing_blob_file_does_not_crash():
    """The record must still read even if its blob is gone — a dead button is worse."""
    sid = cs.register({"company": "Acme", "role": "PM", "jd_text": "x" * 5000})
    os.remove(cs._blob_path(sid))
    assert cs.get(sid)["jd_text"] == ""
