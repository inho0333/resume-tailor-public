"""The failures a review of this repository found, pinned.

Every test here reproduces something that happened in a real run of a clean clone, where the
tool either shipped a quietly worse résumé or sent a prompt describing somebody else. None of
them were caught by the existing suite, which is the only reason they are in a file of their
own: the names say what went wrong, not what the code does.
"""

import os
import sys

import pytest
import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import llm_utils
import minimal_tailor
import profile as profile_module
import resume_parser


# ── a swap must bring in a different bullet ─────────────────────────────────

def _base_and_db():
    """One section, one bullet, and a database holding only that bullet — the state a fresh
    `bootstrap` leaves behind."""
    text = ("Led seed diligence on 40 consumer startups, after the fund lacked a repeatable "
            "screen, by building a bottom-up willingness-to-pay model, deploying $4M across "
            "eight companies with three follow-ons.")
    base = {"sections": [{"name": "EXAMPLE VENTURES", "header_text": "EXAMPLE VENTURES",
                          "bullets": [{"id": "ev-b1", "text": text}]}]}
    db = [{"name": "EXAMPLE VENTURES", "header_text": "EXAMPLE VENTURES",
           "bullets": [{"id": "ev-b1", "text": text, "tags": {}}]}]
    return base, db, text


def test_swap_of_a_bullet_for_itself_is_rejected():
    """The measured failure: with no swap candidates, MIN_SWAPS pushed the model into
    "swapping" a bullet for itself, which validated a rewrite of a base sentence against the
    swap rules (45% word retention, secondary figures allowed to drop) instead of the edit
    rules. The sentence came back without its Why and How clauses and every gate passed it.
    """
    base, db, _ = _base_and_db()
    shortened = ("Led seed diligence on 40 consumer startups, deploying $4M across eight "
                 "companies with three follow-ons.")
    plan = {"edits": [], "swaps": [{"out": "ev-b1", "in": "ev-b1", "text": shortened,
                                    "why": "keeps the section the same length"}]}
    result, applied = minimal_tailor.apply_plan(base, db, plan, log=lambda *a, **k: None)

    assert applied["swaps"] == []
    assert any("in == out" in r["why"] for r in applied["rejected"])
    # And the résumé still carries the approved sentence, clauses intact.
    assert "repeatable screen" in result["sections"]["EXAMPLE VENTURES"][0]["text"]


def test_no_swap_candidates_skips_the_second_request():
    """Asking again cannot invent a candidate. It costs one heavy call and invites the model
    to satisfy MIN_SWAPS dishonestly, so the round ends early instead."""
    base, db, _ = _base_and_db()
    assert minimal_tailor.swap_candidates(base, db) == []

    calls = []

    def fake_plan(*args, **kwargs):
        calls.append(kwargs.get("feedback", ""))
        return {"edits": [], "swaps": [], "keyword_notes": []}

    original = minimal_tailor.plan
    minimal_tailor.plan = fake_plan
    try:
        _result, applied = minimal_tailor.run(object(), base, db, {}, log=lambda *a, **k: None)
    finally:
        minimal_tailor.plan = original

    assert len(calls) == 1, "the plan was requested twice with nothing to swap in"
    assert applied.get("no_swap_candidates") is True


def test_swap_candidates_excludes_base_protected_and_retired():
    base = {"sections": [{"name": "ACME", "header_text": "ACME",
                          "bullets": [{"id": "on-page", "text": "On the résumé already, 10%."}]}]}
    db = [{"name": "ACME", "header_text": "ACME", "bullets": [
        {"id": "on-page", "text": "On the résumé already, 10%.", "tags": {}},
        {"id": "retired-one", "text": "Dead experience, 20%.", "tags": {}, "retired": True},
        {"id": "skills", "text": "Skills: SQL, Python", "tags": {}},
        {"id": "fresh", "text": "Something else entirely, 30%.", "tags": {}},
    ]}]
    assert [b["id"] for b in minimal_tailor.swap_candidates(base, db)] == ["fresh"]


# ── look-alike characters ───────────────────────────────────────────────────

def test_non_breaking_hyphen_is_normalised_not_rejected():
    """A local model returned U+2011 in "working‑capital" and "follow‑ons". It renders
    correctly in Word, breaks in a plain-text ATS paste, and passed every gate because the
    dash check only looked for the em and en dash. It is noise, not a style choice, so it is
    cleaned up rather than costing a good edit."""
    assert llm_utils.normalize_typography("working\u2011capital") == "working-capital"
    assert llm_utils.normalize_typography("a\u00a0b") == "a b"
    assert llm_utils.normalize_typography("minus\u2212sign") == "minus-sign"


def test_em_dash_is_not_normalised_away():
    """The em dash is a style violation the validators must still see and reject — silently
    turning it into a hyphen would defeat the ban."""
    assert "—" in llm_utils.normalize_typography("clause — clause")


def test_check_text_rejects_a_surviving_look_alike_hyphen():
    original = "Migrated billing to a weekly cycle for 12,000 merchants, cutting churn 18%."
    sneaky = "Migrated billing to a weekly\u2011cycle for 12,000 merchants, cutting churn 18%."
    reason = minimal_tailor.check_text(sneaky, original, None)
    assert reason and "non-ASCII dash" in reason


def test_apply_plan_normalises_an_edit_instead_of_losing_it():
    """A good edit carrying a stray non-breaking hyphen is kept, with the character gone — the
    alternative (reject it) throws away a correct edit over invisible noise."""
    original = ("Migrated billing from a monthly to a weekly cycle for 12,000 merchants, by "
                "modelling the working-capital impact and rebuilding settlement, cutting "
                "merchant churn 18%.")
    edited = original.replace("billing", "payouts").replace("working-capital",
                                                            "working\u2011capital")
    assert "\u2011" in edited, "the test input must actually carry the look-alike character"
    base = {"sections": [{"name": "ACME", "header_text": "ACME",
                          "bullets": [{"id": "a1", "text": original}]}]}
    db = [{"name": "ACME", "header_text": "ACME",
           "bullets": [{"id": "a1", "text": original, "tags": {},
                        "context": {"background": "merchant payouts and billing"}}]}]

    result, applied = minimal_tailor.apply_plan(
        base, db, {"edits": [{"id": "a1", "text": edited}], "swaps": []},
        log=lambda *a, **k: None)

    assert [e["id"] for e in applied["edits"]] == ["a1"], applied["rejected"]
    shipped = result["sections"]["ACME"][0]["text"]
    assert "payouts" in shipped
    assert all(ord(ch) < 128 for ch in shipped), "a look-alike character reached the document"


# ── the prompts describe the person in profile.yaml ─────────────────────────

def _profile(**overrides):
    data = yaml.safe_load(open(profile_module.EXAMPLE_PATH, encoding="utf-8"))
    education = data.setdefault("education", {})
    for key, value in overrides.pop("education", {}).items():
        education[key] = value
    data.update(overrides)
    return profile_module.Profile(data)


def test_applicant_phrase_follows_the_degree():
    """The rewrite, review and polish prompts said "an MBA internship applicant" literally.
    For anyone else that is a prompt about a different person, on exactly the calls where
    quality matters."""
    assert _profile().applicant_phrase == "an MBA internship applicant"
    assert _profile(education={"program": "MS Computer Science"}).applicant_phrase == \
        "an MS Computer Science internship applicant"
    assert _profile(education={"program": "Master of Public Policy"}).applicant_phrase == \
        "a Master of Public Policy internship applicant"


def test_applicant_phrase_without_an_internship_search():
    prof = _profile(target_roles=["product manager"], target_term="")
    assert prof.targets_internship is False
    assert prof.applicant_phrase == "an MBA applicant"


def test_no_prompt_hardcodes_a_degree():
    """A literal degree in a prompt template is the bug this guards: profile.py is the only
    place allowed to state a fact about the applicant."""
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for name in ("minimal_tailor.py", "recruiter_review.py", "tailor_engine.py"):
        with open(os.path.join(here, name), encoding="utf-8") as f:
            body = f.read()
        for phrase in ("an MBA internship applicant", "MBA intern\napplicant",
                       "students from top MBA programs"):
            assert phrase not in body, f"{name} still hardcodes {phrase!r}"


def test_prompt_templates_accept_the_applicant_phrase():
    assert "{applicant_phrase}" in minimal_tailor.PLAN_PROMPT_TEMPLATE
    import recruiter_review
    import tailor_engine
    assert "{applicant_phrase}" in recruiter_review.CRITIQUE_PROMPT_TEMPLATE
    assert "{applicant_phrase}" in recruiter_review.POLISH_PROMPT_TEMPLATE
    assert "{applicant_phrase}" in tailor_engine.SELECT_PROMPT_TEMPLATE
    assert "{applicant_phrase}" in tailor_engine.REWRITE_PROMPT_TEMPLATE


# ── identity left at the example's values is blocking, not a warning ────────

def test_identity_and_preference_fields_are_separated():
    example = profile_module.load(path=profile_module.EXAMPLE_PATH)
    identity = example.unedited_identity_fields()
    prefs = example.unedited_preference_fields()
    assert "name" in identity and "education.school" in identity
    assert "style_examples" in prefs
    assert not set(identity) & set(prefs)


def test_validate_inputs_refuses_an_unedited_profile(tmp_path, monkeypatch):
    """`doctor` reporting it is not enough: nobody runs `doctor` after the first day, and the
    run itself would have reported PASS on a résumé judged for a fictional person."""
    import config
    import errors
    import run

    example = profile_module.load(path=profile_module.EXAMPLE_PATH)
    monkeypatch.setattr(profile_module, "get", lambda: example)
    docx_path = tmp_path / "resume.docx"
    docx_path.write_bytes(b"not really a docx, and never opened")
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "db.yaml"))
    (tmp_path / "db.yaml").write_text("sections: []\n", encoding="utf-8")

    with pytest.raises(errors.SetupError) as excinfo:
        run._validate_inputs(str(docx_path))
    assert "profile.example.yaml" in str(excinfo.value)


# ── the shipped template is a document this tool can actually read ──────────

def test_shipped_template_parses_into_experience_blocks():
    """`templates/resume_template.docx` exists so a first-time user is not left to
    reverse-engineer the layout from prose. If it stops parsing, that promise is broken."""
    docx = pytest.importorskip("docx")
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(here, "templates", "resume_template.docx")
    assert os.path.exists(path), "run python templates/make_template.py"

    blocks = resume_parser.parse_experience_blocks(docx.Document(path))
    names = [b["header_text"] for b in blocks]
    assert names == ["ACME CORP., Austin, TX", "EXAMPLE VENTURES, Singapore", "ADDITIONAL"]
    assert all(len(b["bullets"]) == 2 for b in blocks)
    # The Skills line must be one the renderer will leave alone.
    assert any(minimal_tailor.is_protected(text)
               for text in blocks[-1]["bullets"])
