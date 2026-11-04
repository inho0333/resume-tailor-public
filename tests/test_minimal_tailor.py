"""Pinned tests for minimal_tailor (the smallest-possible-edit mode).

Each case below is a real failure, kept as a test so the same KIND of bad edit can
never pass again. The reference is always the approved sentence in
base_resume.yaml; here that is the fixture, tests/fixtures/base_resume.sample.yaml.

The run that produced most of these was judged unusable by hand: sentences
compressed into telegraphic notation, figures silently changed, bullets rewritten
from scratch. The rule each test holds is named in its docstring, and the assertions
check the specific rejection reason, not merely that something was rejected.
"""

import re

import pytest

import minimal_tailor as mt
import resume_writer

# `base`, `db` and `db_map` come from tests/conftest.py, which loads the fictional
# fixtures. Nothing here reads the real (gitignored) career data.


def test_base_ids_all_exist_in_db(base, db_map):
    """Every base bullet has a DB entry — the premise of verifying any edit or swap."""
    for bullet_id in mt.base_index(base):
        assert bullet_id in db_map, f"{bullet_id} in base_resume is missing from resume_db"


# The fixture base measures 2,278 characters across 11 bullets; the window is drawn
# around that measurement, not copied from anywhere.
BASE_TOTAL_RANGE = (2100, 2500)


def test_base_fits_one_page_budget(base):
    """The base total IS the length budget — stay at or under it and one page is
    structurally guaranteed, because the base itself is one page."""
    low, high = BASE_TOTAL_RANGE
    assert low < mt.base_total_chars(base) < high


WORST_CASE = ("Built a bottom-up model at a $400M AUM firm sizing customers x willingness-to-pay "
              "(labor saved, spend on alternatives) x capture rate, held to ~20%/~5% vs 60-70% "
              "claims; invested $12.0M across 27 companies (18 AI-driven), 7 with follow-on.")


def test_worst_case_sentence_is_rejected(base, db_map):
    original = mt.base_index(base)["acme-investment"]["text"]
    assert mt.check_text(WORST_CASE, original, db_map["acme-investment"]) is not None


@pytest.mark.parametrize("bad,expect", [
    # Telegraphic compression: it passes the length check, but every word has changed.
    ("Built a bottom-up model sizing customers x willingness-to-pay x capture rate, held to "
     "~20% vs 60-70% claims; sourced $12.0M across 27 startups (18 AI-driven)",
     "word retention"),
    # A rewrite from scratch.
    ("Owned pricing diligence end to end, validating demand assumptions for 27 portfolio bets "
     "and deploying $12.0M where unit economics held up under scrutiny", "word retention"),
])
def test_rewrites_are_rejected(base, db_map, bad, expect):
    original = mt.base_index(base)["acme-investment"]["text"]
    reason = mt.check_text(bad, original, db_map["acme-investment"])
    assert reason and expect in reason


def test_invented_number_is_rejected(base, db_map):
    original = mt.base_index(base)["acme-investment"]["text"]
    reason = mt.check_text(original.replace("$12.4M", "$9.9M"), original,
                           db_map["acme-investment"])
    assert reason and "figure not in the original or the DB invented" in reason


def test_losing_a_number_is_rejected(base, db_map):
    """Quantified results come first — lose even one figure and the edit is rejected."""
    original = mt.base_index(base)["acme-investment"]["text"]
    reason = mt.check_text(original.replace("33% ", ""), original, db_map["acme-investment"])
    assert reason and "quantified result lost" in reason


def test_longer_edit_is_rejected(base, db_map):
    """An edit cannot get longer: a pointless word or two must not add a line."""
    original = mt.base_index(base)["acme-investment"]["text"]
    reason = mt.check_text(original.replace("startups", "early-stage startups"), original,
                           db_map["acme-investment"])
    assert reason and "too long" in reason


def test_new_proper_noun_is_rejected(base, db_map):
    """Describe things at the higher level — dragging in a proper noun or tool name that
    the original did not have is detail creep, and is rejected."""
    original = mt.base_index(base)["acme-deck-tool"]["text"]
    shorter = original.replace("database and API integration", "Airflow pipelines")
    reason = mt.check_text(shorter, original, db_map["acme-deck-tool"])
    assert reason and "proper noun" in reason


def test_em_dash_is_rejected(base, db_map):
    original = mt.base_index(base)["acme-investment"]["text"]
    reason = mt.check_text(original + " — and more", original, db_map["acme-investment"])
    assert reason and "dash" in reason


def test_length_growth_is_capped(base, db_map):
    original = mt.base_index(base)["example-review-ui"]["text"]
    padded = original + " " + "x" * (mt.MAX_LEN_GROWTH + 5)
    reason = mt.check_text(padded, original, db_map["example-review-ui"])
    assert reason and "too long" in reason


def test_word_level_edit_passes(base, db_map):
    """Substituting a word for the JD's own wording must pass, as long as the sentence does
    not get longer — it is the only edit this mode allows."""
    original = mt.base_index(base)["acme-investment"]["text"]
    edited = original.replace("startups", "SMBs")
    assert mt.check_text(edited, original, db_map["acme-investment"]) is None


def test_skills_line_is_protected(base, db, db_map):
    """The Skills/Languages line is excluded from edits and swaps: its format is fixed."""
    plan = {"edits": [{"id": "add-skills", "text": "Skills: Pricing, Monetization, SQL"}],
            "swaps": []}
    result, applied = mt.apply_plan(base, db, plan, log=lambda *a: None)
    assert not applied["edits"]
    assert any("fixed-format line" in r["why"] for r in applied["rejected"])
    skills = [r for r in result["sections"]["ADDITIONAL"] if r["id"] == "add-skills"][0]
    assert skills["text"] == mt.base_index(base)["add-skills"]["text"]


def _a_base_bullet(base, section="ACME CORP.", exclude=()):
    """One bullet id that is in the base résumé **right now**. Hardcoding an id breaks the
    test the moment that bullet is swapped out of the base, which has actually happened."""
    for s_ in base["sections"]:
        if s_["name"] != section:
            continue
        for b in s_["bullets"]:
            if b["id"] not in exclude:
                return b["id"]
    raise AssertionError(f"no usable bullet in section {section}")


def test_swap_limit_and_section_match(base, db):
    """At most MAX_SWAPS swaps, and only within the same section."""
    plan = {"edits": [], "swaps": [
        {"out": _a_base_bullet(base), "in": "example-ma-screening", "text": "irrelevant"},
    ]}
    _result, applied = mt.apply_plan(base, db, plan, log=lambda *a: None)
    assert not applied["swaps"]
    assert any("section mismatch" in r["why"] for r in applied["rejected"])


def test_empty_plan_keeps_base_verbatim(base, db):
    """No reason to touch it means the base ships verbatim — a forced edit is the worst
    possible outcome."""
    result, applied = mt.apply_plan(base, db, {"edits": [], "swaps": []}, log=lambda *a: None)
    assert not applied["edits"] and not applied["swaps"]
    for section in base["sections"]:
        rendered = result["sections"][section["name"]]
        assert [r["text"] for r in rendered] == [b["text"] for b in section["bullets"]]


def test_growth_edits_are_all_rejected_and_total_stays_in_budget(base, db):
    """Edits that get longer are all rejected individually, so the total can never exceed
    the base."""
    edits = [{"id": b["id"], "text": b["text"] + " in each quarter"}
             for s in base["sections"] for b in s["bullets"] if not mt.is_protected(b["text"])]
    result, applied = mt.apply_plan(base, db, {"edits": edits, "swaps": []}, log=lambda *a: None)
    assert not applied["edits"]
    total = sum(len(r["text"]) for rows in result["sections"].values() for r in rows)
    assert total <= mt.base_total_chars(base) + mt.TOTAL_GROWTH_SLACK


@pytest.mark.parametrize("shortened,ok", [
    ("Built Scout, an AI pitch-deck scoring tool to keep pace with a surging deal pipeline",
     True),
    # "for" is a word the original does not contain, so this is a rewrite, not a deletion.
    ("Built Scout, an AI scoring tool for a surging pipeline", False),
])
def test_deletion_only_check(shortened, ok):
    """Widow compression may **only delete words** — the code catches a rewrite."""
    original = ("Built Scout, an AI pitch-deck scoring tool to keep pace with a surging deal "
                "pipeline, completing database and API integration hands-on")
    assert mt.is_deletion_only(original, shortened) is ok


def test_swap_text_is_checked_against_db_not_removed_bullet(base, db, db_map):
    """A swap sentence is checked against the DB original, not against the bullet leaving the
    page: sharing few words with the outgoing bullet is normal. Checking it against the
    outgoing sentence rejected a perfectly good swap at "word retention 8%"."""
    out_id = _a_base_bullet(base, exclude=("acme-ic-approval",))
    db_text = db_map["acme-ic-approval"]["text"]
    out_text = mt.base_index(base)[out_id]["text"]
    # The DB sentence trimmed to the outgoing bullet's length (most words kept).
    swap_text = db_text[:len(out_text) + mt.MAX_LEN_GROWTH].rsplit(" ", 1)[0]
    plan = {"edits": [], "swaps": [
        {"out": out_id, "in": "acme-ic-approval", "text": swap_text, "why": "t"},
    ]}
    result, applied = mt.apply_plan(base, db, plan, log=lambda *a: None)
    assert applied["swaps"], applied["rejected"]
    ids = [r["id"] for r in result["sections"]["ACME CORP."]]
    assert "acme-ic-approval" in ids and out_id not in ids


def _docx_from_base(base, path):
    """Build a .docx whose bullets get their glyph from the `List Bullet` **style**, with no
    numPr on the paragraph — the shape of the real approved document.

    Built here rather than committed so a fresh clone needs no binary fixture, and so the
    test does not depend on the owner's résumé file.
    """
    import docx

    doc = docx.Document()
    for section in base["sections"]:
        doc.add_paragraph(section["header_text"])
        doc.add_paragraph("Job title | 2020 - 2021")   # the context line, left untouched
        for bullet in section["bullets"]:
            doc.add_paragraph(bullet["text"], style="List Bullet")
    doc.save(str(path))
    return path


def test_style_based_bullets_are_detected(base, tmp_path):
    """Bullets that take their glyph from a `List Bullet` style carry no numPr. Looking only
    for numPr finds no bullet block for the section, and the renderer then writes into the
    NEXT section's block — output identical to the approved document, every edit silently
    gone, and still reported as a pass."""
    import docx as _docx

    path = _docx_from_base(base, tmp_path / "sample_resume.docx")
    doc = _docx.Document(str(path))
    headers = {s["header_text"] for s in base["sections"]}
    for section in base["sections"]:
        _anchor, bullets = resume_writer.find_section_block(
            doc, section["header_text"], stop_texts=headers)
        assert len(bullets) == len(section["bullets"]), section["name"]


# --- Retired bullets are blocked ------------------------------------------------------------
# A retired bullet (the predecessor of a discontinued side project) was swapped in by one
# run. The exclusion signal existed only in the rebuild path at the time, so the minimal path
# was still offering it as a candidate.

RETIRED_DB = [{
    "name": "ADDITIONAL",
    "header_text": "ADDITIONAL",
    "bullets": [
        {"id": "add-live", "text": "Shipped a live product that reached 2,000 daily users",
         "tags": {"keywords": ["product"]}},
        {"id": "add-gone", "text": "Co-founded a shut-down app, leading a team of three",
         "tags": {"keywords": ["entrepreneurship"]}, "retired": True},
    ],
}]


def test_retired_bullet_is_not_offered_as_candidate(base):
    blob = mt._candidates_blob(base, RETIRED_DB)
    assert "add-live" in blob
    assert "add-gone" not in blob


def test_retired_bullet_is_rejected_when_planned(base):
    out_id = next(bid for bid, info in mt.base_index(base).items()
                  if info["section"] == "ADDITIONAL" and not mt.is_protected(info["text"]))
    plan_data = {"edits": [], "swaps": [
        {"out": out_id, "in": "add-gone",
         "text": "Co-founded a shut-down app, leading a team of three", "why": "test"},
    ]}
    _result, applied = mt.apply_plan(base, RETIRED_DB, plan_data, log=lambda *a, **k: None)
    assert applied["swaps"] == []
    assert any(r["id"] == "add-gone" and "retired" in r["why"] for r in applied["rejected"])


# An experience the user has ruled out of the résumé entirely is not merely retired: it is
# deleted, so no path can reach it. `retired: true` keeps a record; this keeps nothing.
BANNED_BULLET_IDS = ("add-banned-project",)
BANNED_TERMS = ("bannedproject",)


def test_banned_experience_is_absent_from_the_db(db_map):
    """A ruled-out experience must be gone from the DB, not just flagged."""
    for bullet_id in BANNED_BULLET_IDS:
        assert bullet_id not in db_map
    for bullet in db_map.values():
        for term in BANNED_TERMS:
            assert term not in bullet["text"].lower()


def test_base_numbers_are_backed_by_db(base, db_map):
    """Every figure in a base sentence must have a source in the DB (text + context).

    One base sentence said "saved 30 hours" while the DB impact said "~25 hours", and the
    recruiter review raised "the figures do not agree" as its top finding on every single
    run. This catches the two drifting apart again.
    """
    for bullet_id, info in mt.base_index(base).items():
        if mt.is_protected(info["text"]):
            continue
        sources = [db_map.get(bullet_id)] + [db_map.get(i) for i in info["merged_ids"]]
        allowed = mt._allowed_numbers(info["text"], sources)
        missing = [n for n in mt._numbers(info["text"]) if n not in allowed]
        assert not missing, f"figures {missing} in base bullet {bullet_id} are not in the DB"


# --- One or two swaps are guaranteed, in the company's vocabulary ---------------------------
# The instruction behind this: "swap one or two bullets on top of the latest version, and
# change the wording to words that fit the JD or the company's philosophy while keeping the
# meaning". A second-pass run that ended with zero swaps is where these came from.

class _ScriptedBackend:
    """Returns pre-set replies in order, and keeps the prompts it was given."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.prompts = []

    def complete(self, prompt):
        self.prompts.append(prompt)
        return self.replies.pop(0) if self.replies else "{}"


def _swap_reply(out_id, in_id, text):
    import json
    return json.dumps({"edits": [], "swaps": [{"out": out_id, "in": in_id, "text": text,
                                               "why": "closer to the JD"}]})


def _additional_swap_pair(base, db):
    """Pick an (outgoing base id, incoming DB id, incoming sentence) that actually passes the
    code's checks.

    Feeding a DB sentence in verbatim is usually rejected on length or an em dash — the swap
    sentence is normally written by the LLM — so a candidate that passes is searched for. If
    none exists the fixture has drifted, which is a failure, not a reason to skip.
    """
    idx = mt.base_index(base)
    in_base = set(idx)
    for section in db:
        rows = [b for b in mt._section_bullets(base, section["name"])
                if not mt.is_protected(b["text"])]
        if not rows:
            continue
        section_max = max(len(b["text"]) for b in rows)
        out_id = next((bid for bid, info in idx.items()
                       if info["section"] == section["name"] and not mt.is_protected(info["text"])),
                      None)
        if not out_id:
            continue
        for bullet in section["bullets"]:
            if bullet["id"] in in_base or mt.is_protected(bullet["text"]) or bullet.get("retired"):
                continue
            if mt.check_text(bullet["text"], bullet["text"], bullet,
                             min_overlap=mt.MIN_WORD_OVERLAP_SWAP, max_len=section_max) is None:
                return out_id, bullet["id"], bullet["text"]
    raise AssertionError("the fixture DB has no swap candidate that passes the checks")


def test_plan_prompt_demands_one_or_two_swaps_and_company_voice(base, db):
    backend = _ScriptedBackend(['{"edits": [], "swaps": []}', '{"edits": [], "swaps": []}'])
    jd_profile = {"company": "Globex Industries", "culture_values": ["customer obsession"],
                  "company_vocabulary": ["Workflow Cloud"]}
    mt.run(backend, base, db, jd_profile, log=lambda *a, **k: None)
    prompt = backend.prompts[0]
    assert f"**{mt.MIN_SWAPS}-{mt.MAX_SWAPS}**" in prompt
    assert "Workflow Cloud" in prompt and "customer obsession" in prompt
    assert "Keep the meaning exactly as it is" in prompt


def test_zero_swaps_triggers_one_retry(base, db):
    out_id, in_id, text = _additional_swap_pair(base, db)
    backend = _ScriptedBackend(['{"edits": [], "swaps": []}', _swap_reply(out_id, in_id, text)])
    _result, applied = mt.run(backend, base, db, {}, log=lambda *a, **k: None)
    assert len(backend.prompts) == 2, "zero swaps must trigger one more plan request"
    assert "[First plan rejected]" in backend.prompts[1]
    assert [sw["in"] for sw in applied["swaps"]] == [in_id]


def test_retry_happens_only_once_and_falls_back_to_base(base, db):
    backend = _ScriptedBackend(['{"edits": [], "swaps": []}', '{"edits": [], "swaps": []}'])
    _result, applied = mt.run(backend, base, db, {}, log=lambda *a, **k: None)
    assert len(backend.prompts) == 2, "at most one retry (LLM calls are not burned endlessly)"
    assert applied["swaps"] == []
    assert applied.get("no_swap") is True


def test_valid_swap_on_first_try_does_not_retry(base, db):
    out_id, in_id, text = _additional_swap_pair(base, db)
    backend = _ScriptedBackend([_swap_reply(out_id, in_id, text)])
    _result, applied = mt.run(backend, base, db, {}, log=lambda *a, **k: None)
    assert len(backend.prompts) == 1
    assert len(applied["swaps"]) == 1


def test_swap_that_repeats_a_kept_bullet_is_rejected(base, db_map):
    """A swap that stretches one experience across two lines is rejected — measured on a
    bullet that came in repeating the kept bullet's figures verbatim."""
    kept = mt.base_index(base)["example-claim2"]["text"]
    assert mt._duplicate_of_kept(db_map["example-claim2-backend"]["text"], [kept])


def test_swap_replacing_the_duplicated_bullet_is_allowed(base, db_map):
    """Being the same experience as the bullet **leaving** the page is allowed — that is a
    swap to a different angle on the same work."""
    idx = mt.base_index(base)
    kept = [info["text"] for bid, info in idx.items() if bid != "acme-investment"]
    assert mt._duplicate_of_kept(db_map["acme-investment-3d"]["text"], kept) is None


# --- A swap is judged against the base résumé's absolute style caps ------------------------
# Once one swap per run was guaranteed, four proposals in a row fell on "word count went up"
# and "quantified result lost ['35']" and the run ended with zero swaps. Both of those are
# rules for editing the SAME sentence, and they were being applied to swaps.

def test_swap_may_be_longer_than_the_terse_db_line(db_map):
    """A swap sentence may use more words than the one-line DB entry (its length and word
    caps are taken from the base résumé instead)."""
    db_bullet = db_map["example-quarterly-research"]
    longer = db_bullet["text"].rstrip(".") + ", surveying 500+ respondents per cycle"
    assert mt.check_text(longer, db_bullet["text"], db_bullet,
                         min_overlap=mt.MIN_WORD_OVERLAP_SWAP, max_len=400,
                         swap=True, max_words=60) is None
    # As an edit (swap=False) the same sentence is rejected for breaking concision.
    assert mt.check_text(longer, db_bullet["text"], db_bullet, max_len=400) is not None


def test_swap_must_keep_at_least_one_number(db_map):
    db_bullet = db_map["acme-ic-approval"]
    stripped = re.sub(r"\d[\d,.]*%?", "", db_bullet["text"])
    reason = mt.check_text(stripped, db_bullet["text"], db_bullet,
                           min_overlap=mt.MIN_WORD_OVERLAP_SWAP, max_len=400,
                           swap=True, max_words=60)
    assert reason == "no quantified result (the swap sentence has no figure at all)"


def test_swap_respects_base_style_caps(db_map):
    db_bullet = db_map["example-quarterly-research"]
    noisy = db_bullet["text"] + " (a); (b); (c), (d), (e), (f)"
    assert mt.check_text(noisy, db_bullet["text"], db_bullet,
                         min_overlap=mt.MIN_WORD_OVERLAP_SWAP, max_len=500,
                         swap=True, max_words=90) is not None
