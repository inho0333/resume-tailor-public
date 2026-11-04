"""Shared test fixtures, and the reason the suite runs on a fresh clone.

The three files the pipeline reads about a person — `profile.yaml`,
`resume_db.yaml`, `base_resume.yaml` — are gitignored, so none of them exists in a
clone. Everything here points the code at the fictional equivalents in
`tests/fixtures/` instead:

    profile.sample.yaml       who the applicant is (degree, roles, voice)
    resume_db.sample.yaml     the experience database
    base_resume.sample.yaml   the approved-résumé subset of that database

`profile.get()` caches process-wide and `profile.PROFILE_PATH` is read at import
time, so the profile is installed by setting the module cache directly and clearing
it again afterwards. That is the only route that reaches code which has already
imported the module.

Nothing in the suite may reach the network or a model CLI. Telegram's sender is
stubbed for every test, because `TG_BOT_TOKEN` / `TG_CHAT_ID` are set in a real
user's environment and `vendor.telegram.send` would otherwise post for real; LLM
backends are always the fake ones the test files define.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import minimal_tailor as mt
import notify
import profile as profile_module
import resume_db
import vendor.telegram

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")

PROFILE_PATH = os.path.join(FIXTURES, "profile.sample.yaml")
DB_PATH = os.path.join(FIXTURES, "resume_db.sample.yaml")
BASE_PATH = os.path.join(FIXTURES, "base_resume.sample.yaml")


@pytest.fixture(scope="session", autouse=True)
def sample_profile():
    """Make `profile.get()` return the fictional profile, for the whole session.

    Anything that builds a prompt calls `profile.get()` (fit_gate's role patterns and
    degree regex, jd_extractor's eligibility screen, tailor_engine's reference
    style), and without this every one of them raises ProfileError.
    """
    loaded = profile_module.load(path=PROFILE_PATH)
    profile_module._cached = loaded
    yield loaded
    profile_module.reset_cache()


@pytest.fixture(autouse=True)
def no_telegram(monkeypatch):
    """No test sends a message, whether or not the environment has credentials.

    Every sender is replaced with the no-op it already is when `TG_BOT_TOKEN` /
    `TG_CHAT_ID` are unset, so the suite behaves identically on a machine that has
    credentials and on one that does not. `notify.send_message` is stubbed as well
    as `vendor.telegram.send`: it posts to the API itself rather than going through
    the vendor module.
    """
    def _not_sent(*_args, **_kwargs):
        return False

    monkeypatch.setattr(vendor.telegram, "send", _not_sent)
    monkeypatch.setattr(notify, "send_message", _not_sent)
    monkeypatch.setattr(notify, "send_document", _not_sent)


@pytest.fixture(scope="session")
def base():
    """The approved résumé, as data. Read-only — do not mutate it in a test."""
    return mt.load_base(BASE_PATH)


@pytest.fixture(scope="session")
def db():
    """The experience database sections. Read-only — do not mutate them in a test."""
    return resume_db.load_db(DB_PATH)


@pytest.fixture(scope="session")
def db_map(db):
    return resume_db.bullets_by_id(db)
