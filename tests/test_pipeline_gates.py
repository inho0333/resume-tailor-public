"""The gates between a posting and a draft: pre-fit judgment, digest, recruiter polish,
two-stage selection, and the generation lock.

Every case here pins a **measured failure**:
  - a digest body of 4,259 characters, over Telegram's 4,096 limit, so **the whole message
    never arrived** and that was the day's only output;
  - an eligibility screen reporting "does not meet the MBA requirement" for a posting
    literally titled "Product Manager **MBA Intern**", which routed it to the silent pile;
  - a small local model handing out "strong fit" freely (two operations-analyst internships
    in one batch, both "high", both ignored until they expired);
  - a polish path that could invent a figure the sentence and the database never had.
"""

import os
import tempfile

import pytest

import config
import confirm_state as cs
import decisions
import digest
import fit_gate
import genlock
import jd_extractor
import notify
import profile as profile_module
import recruiter_review
import resume_db
import tailor_engine


@pytest.fixture(autouse=True)
def _tmp_state(monkeypatch):
    """Move every state file into a temporary directory, so a test never touches the real
    pending approvals or the real decision journal."""
    with tempfile.TemporaryDirectory() as d:
        monkeypatch.setattr(config, "STATE_DIR", d)
        monkeypatch.setattr(config, "PENDING_CONFIRMATIONS_PATH", os.path.join(d, "p.json"))
        monkeypatch.setattr(cs, "_STATE_FILE", os.path.join(d, "p.json"))
        monkeypatch.setattr(decisions, "PATH", os.path.join(d, "decisions.jsonl"))
        monkeypatch.setattr(genlock, "LOCK_PATH", os.path.join(d, "gen.lock"))
        yield d


def _record(company, role, tier="mid", verdict="yes", quote="", unmet=None, url=None):
    return {
        "company": company, "role": role, "url": url,
        "jd_profile": {"role": role, "company": company, "must_have_skills": ["A", "B"]},
        "eligibility": {"degree_eligible": verdict, "quoted_requirement": quote,
                        "reasoning": "test"},
        "prefit": {"tier": tier, "reason": "test judgment", "unmet": unmet or []},
    }


# ── pre-fit judgment ──────────────────────────────────────────────────────────

def test_low_tier_is_not_asked():
    assert fit_gate.should_ask({"tier": "high"})
    assert fit_gate.should_ask({"tier": "mid"})
    assert not fit_gate.should_ask({"tier": "low"})
    assert fit_gate.should_ask(None)  # no judgment = an older record -> ask (fail-open)


def test_high_requires_title_pattern():
    """The local model hands out "high" freely, so without a title match it drops to mid."""
    assert fit_gate._clamp("high", "strong") == ("high", None)
    assert fit_gate._clamp("high", None)[0] == "mid"
    assert fit_gate._clamp("high", "weak")[0] == "mid"


def test_strong_title_floors_at_mid():
    """A title matching a role the applicant actually applies to is asked about even when the
    local model says "low".

    The patterns come from `target_roles` / `avoid_roles` in the profile — here
    tests/fixtures/profile.sample.yaml, installed by conftest — not from constants in
    fit_gate.
    """
    assert fit_gate._clamp("low", "strong")[0] == "mid"
    assert fit_gate._pattern_signal("Product Manager MBA Intern") == "strong"
    assert fit_gate._pattern_signal("Corporate Strategy Intern") == "strong"
    assert fit_gate._pattern_signal("Intern - Scheduling") == "weak"
    assert fit_gate._pattern_signal("Warehouse Associate Intern") == "weak"


def test_pattern_signal_follows_the_profile(monkeypatch):
    """The signal is a statement about what *this* applicant wants, so it has to move when
    the profile moves. It used to be a pair of module constants, which silently judged a
    second user's postings against the first user's taste."""
    custom = profile_module.Profile({
        "target_roles": ["data science"],
        "avoid_roles": ["field sales"],
    })
    monkeypatch.setattr(profile_module, "_cached", custom)
    assert fit_gate._pattern_signal("Data Science Intern") == "strong"
    assert fit_gate._pattern_signal("Field Sales Intern") == "weak"
    # A title the fixture profile calls strong means nothing to this one.
    assert fit_gate._pattern_signal("Corporate Strategy Intern") is None


def test_eligibility_no_is_overridden_when_posting_says_mba():
    """Measured: a posting titled "Product Manager MBA Intern" was filed as not meeting the
    MBA requirement, because the screen quoted a degree line from elsewhere on the page."""
    jd = {"role": "Product Manager MBA Intern, Enterprise Platform"}
    el = {"degree_eligible": "no", "quoted_requirement": "BS or MS degree in Computer Science"}
    assert fit_gate.eligibility_contradicted(jd, el)

    jd2 = {"role": "Product Management Co-Op"}
    el2 = {"degree_eligible": "no",
           "quoted_requirement": "pursuing an MS in Electrical Engineering or MBA"}
    assert fit_gate.eligibility_contradicted(jd2, el2)

    # A genuine mismatch (undergraduate only, or another major required) is not overturned.
    jd3 = {"role": "Supply Chain Management Intern"}
    el3 = {"degree_eligible": "no", "quoted_requirement": "Supply Chain Management major"}
    assert fit_gate.eligibility_contradicted(jd3, el3) is None


def test_should_ask_record_combines_fit_and_eligibility():
    assert fit_gate.should_ask_record(_record("A", "PM Intern"))
    assert not fit_gate.should_ask_record(_record("A", "PM Intern", tier="low"))
    assert not fit_gate.should_ask_record(_record("A", "Nursing Intern", verdict="no"))
    override = _record("Globex", "Product Manager MBA Intern", verdict="no")
    override["prefit"]["eligibility_override"] = True
    assert fit_gate.should_ask_record(override)


# ── digest ────────────────────────────────────────────────────────────────────

def test_digest_stays_under_telegram_limit():
    """Measured: rendering 24 pending postings in full detail came to 4,259 characters, over
    the 4,096 limit, and **the send itself failed**."""
    long_reason = "A fit rationale sentence that happens to run long, as they do. " * 6
    for i in range(30):
        cs.register(_record(f"Company With A Long Name {i}",
                            f"Product Manager MBA Intern, Division {i}",
                            unmet=[long_reason[:80], long_reason[:80]],
                            url=f"https://example.com/jobs/{i}/very/long/path/to/posting"))
        state = cs._load()
        for r in state.values():
            if isinstance(r, dict) and r.get("prefit"):
                r["prefit"]["reason"] = long_reason[:180]
        cs._atomic_write(cs._STATE_FILE, state)

    text, keyboard, asked, listed = digest.build(cs.all_records())
    assert len(text) <= 4096
    assert keyboard and asked
    assert len(keyboard) <= config.DIGEST_MAX_ITEMS


def test_digest_separates_asked_and_silent():
    cs.register(_record("Good", "Product Manager MBA Intern", tier="high"))
    cs.register(_record("Meh", "Warehouse Associate Intern", tier="low"))
    cs.register(_record("Nope", "Nursing Intern", verdict="no", tier="low"))

    text, keyboard, asked, listed = digest.build(cs.all_records())
    assert len(asked) == 1 and len(listed) == 2
    assert "2 not asked about" in text
    # The silent ones keep a "draft it anyway" button: a wrong verdict has to be overturnable.
    assert any("Draft anyway" in label for row in keyboard for label, _ in row)


def test_digest_skips_already_sent_and_expired():
    sid = cs.register(_record("Sent", "Product Manager MBA Intern"))
    cs.update(sid, digest_sent_at="2020-01-01T09:00:00")
    text, _kb, asked, _listed = digest.build(cs.all_records())
    assert text is None and asked == []


def test_digest_empty_is_silent():
    assert digest.build({}) == (None, None, [], [])


# ── the decision journal ──────────────────────────────────────────────────────

def test_decisions_journal_roundtrip():
    r = _record("Globex", "MBA Intern, Summer 2027", tier="high")
    decisions.record("make", r)
    decisions.record("skip", _record("Nope", "Warehouse Associate Intern", tier="low"))
    decisions.record("expire", _record("Gone", "Scheduling Intern", tier="low"), source="prune")

    s = decisions.summary()
    assert s["counts"] == {"make": 1, "skip": 1, "expire": 1}
    lines = decisions.fewshot_lines()
    assert any("Globex" in l and "true" in l for l in lines)
    assert any("Nope" in l and "false" in l for l in lines)


# ── recruiter polish: the anti-fabrication line ───────────────────────────────

class _FakeBackend:
    def __init__(self, payload):
        self.payload = payload

    def complete(self, prompt):
        return self.payload


def _db():
    return [{
        "name": "ACME CORP.", "header_text": "ACME CORP.",
        "bullets": [{
            "id": "b1", "text": "Sourced $12.4M across 28 startups", "tags": {},
            "context": {"why": "w", "how": "h",
                        "impact": "33% follow-on rate, 9 secured follow-on"},
        }],
    }]


def _review_data():
    return {"screening_score": 5, "verdict": "borderline", "first_impression": "x",
            "competitive_context": "y", "top_concerns": ["z"], "missing_signal": [],
            "ats_misses": [],
            "bullet_feedback": [{"id": "b1", "priority": 1, "problem": "weak lead",
                                 "fix_instruction": "put the scale first"}]}


def test_polish_rejects_invented_numbers():
    """A figure that is in neither the sentence nor the DB means that bullet is discarded and
    the original kept."""
    sections = {"ACME CORP.": [{"id": "b1", "text": "Sourced $12.4M across 28 startups"}]}
    backend = _FakeBackend('{"sections": {"ACME CORP.": [{"id": "b1", '
                           '"text": "Invested $25.0M across 99 startups, a 3x return"}]}}')
    out, applied = recruiter_review.polish(backend, "C", "R", _review_data(), sections, _db(),
                                           log=lambda *a: None)
    assert applied == []
    assert out["ACME CORP."][0]["text"] == "Sourced $12.4M across 28 startups"


def test_polish_accepts_numbers_from_db_context():
    """A figure present in `context` may be pulled up into the sentence — that is polish's
    main way of improving one."""
    sections = {"ACME CORP.": [{"id": "b1", "text": "Sourced $12.4M across 28 startups"}]}
    backend = _FakeBackend('{"sections": {"ACME CORP.": [{"id": "b1", '
                           '"text": "Deployed $12.4M into 28 startups, 33% follow-on rate"}]}}')
    out, applied = recruiter_review.polish(backend, "C", "R", _review_data(), sections, _db(),
                                           log=lambda *a: None)
    assert len(applied) == 1
    assert "33% follow-on" in out["ACME CORP."][0]["text"]


def test_polish_rejects_em_dash_and_overlong():
    sections = {"ACME CORP.": [{"id": "b1", "text": "Sourced $12.4M across 28 startups"}]}
    em = _FakeBackend('{"sections": {"ACME CORP.": [{"id": "b1", '
                      '"text": "Sourced $12.4M — across 28 startups"}]}}')
    out, applied = recruiter_review.polish(em, "C", "R", _review_data(), sections, _db(),
                                           log=lambda *a: None)
    assert applied == []

    long_text = "Sourced $12.4M across 28 startups " + "x" * 300
    lo = _FakeBackend('{"sections": {"ACME CORP.": [{"id": "b1", "text": "%s"}]}}' % long_text)
    out, applied = recruiter_review.polish(lo, "C", "R", _review_data(), sections, _db(),
                                           log=lambda *a: None)
    assert applied == []


def test_review_drops_feedback_for_unknown_ids():
    payload = ('{"screening_score": 12, "verdict": "maybe", "first_impression": "i",'
               ' "competitive_context": "c", "top_concerns": ["a"],'
               ' "bullet_feedback": [{"id": "nope", "priority": 1, "problem": "p",'
               ' "fix_instruction": "f"}, {"id": "b1", "priority": 9, "problem": "p2",'
               ' "fix_instruction": "f2"}]}')
    sections = {"ACME CORP.": [{"id": "b1", "text": "Sourced $12.4M across 28 startups"}]}
    data = recruiter_review.review(_FakeBackend(payload), "C", "R", {"track": "Tech"},
                                   sections, _db(), log=lambda *a: None)
    assert data["screening_score"] == 10        # clamped to 1-10
    assert data["verdict"] is None              # a verdict outside the allowed set is dropped
    assert [f["id"] for f in data["bullet_feedback"]] == ["b1"]
    assert data["bullet_feedback"][0]["priority"] == 3   # clamped to 1-3


# ── two-stage selection ───────────────────────────────────────────────────────

def test_select_blob_excludes_context_rewrite_blob_includes_it():
    db = _db()
    select = resume_db.to_select_blob(db)
    rewrite = resume_db.to_rewrite_blob(db, ["b1"])
    assert "Sourced $12.4M" in select and "Background (why)" not in select
    assert "Background (why)" in rewrite and "Outcome (impact)" in rewrite
    assert resume_db.to_rewrite_blob(db, []) == ""   # nothing selected, empty blob


def test_rewrite_falls_back_to_db_text_when_llm_omits_bullet():
    """An id missing from the rewrite result falls back to the DB sentence — better than a
    bullet vanishing from the page."""
    db = _db()
    backend = _FakeBackend('{"sections": {"ACME CORP.": []}}')
    out = tailor_engine.rewrite_bullets(backend, {"track": "Tech"}, db, {"ACME CORP.": ["b1"]},
                                        log=lambda *a: None)
    assert out["ACME CORP."][0]["text"] == "Sourced $12.4M across 28 startups"


def test_select_rejects_unknown_and_retired_ids():
    db = _db()
    db[0]["bullets"].append({"id": "b2", "text": "retired one", "tags": {}, "retired": True})
    backend = _FakeBackend('{"sections": {"ACME CORP.": ["b1", "b2", "ghost"], '
                           '"Nope": ["b1"]}}')
    out = tailor_engine.select_bullets(backend, {"track": "Tech"}, db, log=lambda *a: None)
    assert out["sections"] == {"ACME CORP.": ["b1"]}


# ── generation is serialised ──────────────────────────────────────────────────

def test_generation_lock_is_exclusive():
    with genlock.GenerationLock(label="first"):
        with pytest.raises(genlock.LockBusy):
            with genlock.GenerationLock(label="second", timeout_sec=0):
                pass
    # Once the first lock is released it can be taken again.
    with genlock.GenerationLock(label="third", timeout_sec=0):
        pass


def _db_with_skills():
    db = _db()
    db.append({
        "name": "ADDITIONAL", "header_text": "ADDITIONAL",
        "bullets": [{"id": "add-skills", "text": "Skills: Data Analysis (SQL, Python)",
                     "tags": {}, "label": "Skills"}],
    })
    return db


def test_polish_never_touches_protected_lines():
    """Measured: told by the recruiter that "the AI depth is not visible", polish rebuilt the
    Skills line from 125 to 240 characters and reordered its items. That line's format is
    fixed by the user's own instruction."""
    sections = {
        "ACME CORP.": [{"id": "b1", "text": "Sourced $12.4M across 28 startups"}],
        "ADDITIONAL": [{"id": "add-skills", "text": "Skills: Data Analysis (SQL, Python)"}],
    }
    review = _review_data()
    review["bullet_feedback"].append({"id": "add-skills", "priority": 1,
                                      "problem": "no AI capability visible",
                                      "fix_instruction": "add AI"})
    backend = _FakeBackend('{"sections": {"ADDITIONAL": [{"id": "add-skills", '
                           '"text": "Skills: AI/Product (LLM, prototyping) | Data (SQL)"}]}}')
    out, applied = recruiter_review.polish(backend, "C", "R", review, sections,
                                           _db_with_skills(), log=lambda *a: None)
    assert applied == []
    assert out["ADDITIONAL"][0]["text"] == "Skills: Data Analysis (SQL, Python)"


def test_polish_reverts_growth_when_over_budget():
    """Measured: polish grew every bullet by 10-15 characters, the total went 199 over budget
    and the page estimate flipped to a +2.0 line FAIL. Widow compression cannot undo growth
    spread across every line, so the budget has to be held here."""
    db = _db()
    db[0]["bullets"].append({
        "id": "b2", "text": "Launched the cohort programme with 4 teams", "tags": {},
        "context": {"impact": "500 applications"},
    })
    sections = {"ACME CORP.": [{"id": "b1", "text": "Sourced $12.4M across 28 startups"},
                               {"id": "b2",
                                "text": "Launched the cohort programme with 4 teams"}]}
    before_total = sum(len(i["text"]) for i in sections["ACME CORP."])
    review = _review_data()
    review["bullet_feedback"].append({"id": "b2", "priority": 1, "problem": "p",
                                      "fix_instruction": "f"})
    backend = _FakeBackend(
        '{"sections": {"ACME CORP.": ['
        '{"id": "b1", "text": "Sourced $12.4M across 28 startups, 9 secured follow-on"},'
        '{"id": "b2", "text": "Launched the cohort programme with 4 teams, '
        '500 applications"}]}}')
    # 20 characters of slack: the two corrections (+21 and +18) do not both fit, so the
    # larger one is reverted and the smaller survives.
    out, applied = recruiter_review.polish(backend, "C", "R", review, sections, db,
                                           total_chars_budget=before_total + 20,
                                           log=lambda *a: None)
    total = sum(len(i["text"]) for i in out["ACME CORP."])
    assert total <= before_total + 20
    assert len(applied) == 1


def test_polish_keeps_original_when_llm_returns_empty_text():
    """Returning an empty text, on the grounds that the bullet was "merged into another one",
    must not lose the sentence."""
    sections = {"ACME CORP.": [{"id": "b1", "text": "Sourced $12.4M across 28 startups"}]}
    backend = _FakeBackend('{"sections": {"ACME CORP.": [{"id": "b1", "text": ""}]}}')
    out, applied = recruiter_review.polish(backend, "C", "R", _review_data(), sections, _db(),
                                           log=lambda *a: None)
    assert applied == []
    assert out["ACME CORP."][0]["text"] == "Sourced $12.4M across 28 startups"


# --- the sponsorship-exclusion phrase -------------------------------------------------------
# A posting's "without the need for current or future company sponsorship" was read straight
# past by the local model, and the report went out saying work authorisation was fine. That
# sentence was the real reason the application could not go anywhere.

def test_sponsorship_exclusion_is_detected():
    found = jd_extractor.detect_sponsorship_exclusion(
        "Candidates must be authorized to work in the United States without the need for "
        "current or future company sponsorship.")
    assert found["excluded"] is True
    assert "current or future" in found["quote"]


def test_other_exclusion_phrasings_are_detected():
    for text in ["This role is not eligible for visa sponsorship.",
                 "Globex will not provide visa sponsorship for this position.",
                 "No visa sponsorship is available for this role.",
                 "Applicants must be legally authorized to work in the US without sponsorship "
                 "now or in the future."]:
        assert jd_extractor.detect_sponsorship_exclusion(text)["excluded"] is True, text


def test_sponsoring_employers_and_form_questions_are_not_flagged():
    for text in ["Do you now or will you in the future require immigration sponsorship?",
                 "Globex has a track record of offering H1B sponsorships.",
                 "We will sponsor qualified candidates for H-1B visas.",
                 "Visa sponsorship is available for this position.",
                 "We are an equal opportunity employer and value diversity."]:
        assert jd_extractor.detect_sponsorship_exclusion(text)["excluded"] is False, text


def test_report_shows_sponsorship_exclusion_above_work_authorisation():
    """The sponsorship exclusion is the harder blocker, so it replaces the reassuring
    work-authorisation line rather than sitting next to it."""
    report = notify.Report("Globex", "MBA Intern")
    report.set_eligibility({"degree_eligible": "yes", "visa_caution": False,
                            "sponsorship_excluded": True,
                            "sponsorship_quote": "...without the need for current or future "
                                                 "company sponsorship."})
    body = "\n".join(text for _priority, text in report._blocks())
    assert "excludes candidates who will need sponsorship" in body
    assert "nothing in the posting rules you out" not in body
