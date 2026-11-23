"""The screening axes, and the four kinds of person they have to work for.

The first version of this tool asked one question — how much of this posting can
this career evidence — and clamped the answer on whether a target role appeared
in the job title. Two things went wrong, both invisible:

- a posting in the wrong country, at the wrong level, for the wrong employment
  type, or paying under half what the applicant would accept arrived looking
  exactly like a good one, because none of those facts was extracted and none
  was compared
- "strong fit" was unreachable unless a `target_roles` string appeared literally
  in the title, so a career-changer's postings were all flattened to "possible
  fit" and the digest's ordering carried no information

Every test here is one of those, pinned. The rule they all share: **an axis
nobody set is not screened**, so a sparse profile must produce no violations at
all.
"""

import contextlib
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fit_gate
import profile as profile_module


@contextlib.contextmanager
def _installed(prof):
    """Make `profile.get()` return `prof` for the duration of the block.

    Restores whatever was cached rather than clearing it: conftest installs the
    fixture profile once for the whole session, and `reset_cache()` would leave
    every later test to load a profile.yaml that does not exist in a clone.
    """
    previous = profile_module._cached
    profile_module._cached = prof
    try:
        yield prof
    finally:
        profile_module._cached = previous


def _profile(**overrides):
    """A v2 profile from parts. Only what a test names is set."""
    data = {
        "name": "Test Person",
        "background": {"kind": "experienced", "years_experience": None,
                       "summary": "Some work.", "employers": ["Acme"],
                       "education": {}},
        "search": {}, "market": {}, "authorization": {}, "voice": {},
    }
    for block, values in overrides.items():
        if isinstance(values, dict) and block in data and isinstance(data[block], dict):
            data[block].update(values)
        else:
            data[block] = values
    return profile_module.Profile(data)


def _axes(profile, **jd):
    return [v["message"] for v in fit_gate.check_hard_axes(jd, profile)
            if not v.get("advisory")]


# ── the four people ─────────────────────────────────────────────────────────

def test_a_senior_engineer_is_not_described_as_a_student():
    """The recruiter persona's whole job is comparison, so the comparison set has
    to be real. v1 rendered everyone as a student — and `doctor` asked an
    experienced hire to fill in a degree programme to clear a warning, which then
    told the prompt the pile was made of students from that programme."""
    prof = _profile(background={"kind": "experienced", "years_experience": 9},
                    search={"kind": "full_time", "seniority": ["staff"]})
    assert "student" not in prof.degree_label
    assert "student" not in prof.applicant_phrase
    assert "student" not in prof.peer_pool_phrase
    assert prof.search_phrase == "full-time postings"
    assert "background.education.program" not in prof.missing_fields()


def test_a_new_grad_search_accepts_a_full_time_posting():
    """A new-graduate search is served by a posting labelled full-time, and a
    full-time search by one labelled new grad. Only genuine conflicts count."""
    prof = _profile(search={"kind": "new_grad"})
    assert _axes(prof, employment_type="full_time") == []
    assert _axes(prof, employment_type="new_grad") == []
    assert len(_axes(prof, employment_type="internship")) == 1


def test_a_career_changer_can_still_reach_a_strong_fit():
    """The failure this exists for: `high` required a target role in the TITLE, so
    anyone whose next job is not titled like their last one had every posting
    flattened to "possible fit" and the digest's ordering said nothing.

    A target role found in the posting's own description of the work now counts.
    It does not rescue a posting the model tiered low — the title and the model
    both declined to call that a match."""
    prof = _profile(search={"kind": "full_time", "target_roles": ["product manage"]})
    with _installed(prof):
        jd = {"role": "Business Lead, Payments", "company": "Acme",
              "responsibilities": ["own the product roadmap for payments",
                                   "product management for the merchant side"]}
        signal = fit_gate._pattern_signal(jd["role"], jd["company"],
                                          fit_gate._body_blob(jd))
        assert signal == "body"
        assert fit_gate._clamp("high", signal, [])[0] == "high"
        # but a body match alone does not raise a low verdict
        assert fit_gate._clamp("low", signal, [])[0] == "low"
        # and with no match anywhere, high is still pulled down
        assert fit_gate._clamp("high", None, [])[0] == "mid"


def test_a_non_us_applicant_gets_their_own_sponsorship_wording():
    """A scan carrying only US phrasings reports a UK posting as clean: an employer
    there writes "Certificate of Sponsorship", not "H-1B"."""
    import jd_extractor

    uk = ("We are unable to provide a Certificate of Sponsorship for this role. "
          "Applicants must have an existing right to work in the UK.")
    with _installed(_profile(market={"country": "GB"})):
        assert jd_extractor.detect_sponsorship_exclusion(uk)["excluded"] is True

    # The country-neutral patterns still catch the common wording for everyone,
    # including a profile that names no country at all — the posting whose real
    # disqualifier is that one sentence is too expensive to make conditional.
    plain = ("Candidates must be authorized to work in the United States without "
             "the need for current or future company sponsorship.")
    for prof in (_profile(market={"country": "US"}), _profile()):
        with _installed(prof):
            assert jd_extractor.detect_sponsorship_exclusion(plain)["excluded"] is True


# ── the sponsorship and credential scans ────────────────────────────────────
#
# These are regex rather than a model because a local model read straight past
# *"must be authorized to work without the need for current or future company
# sponsorship"* and reported no concern — and that sentence was the whole reason
# the application could go nowhere.

_SPONSORSHIP_CASES = [
    # (country, sentence, excluded?)
    ("US", "Candidates must be authorized to work in the United States without the "
           "need for current or future company sponsorship.", True),
    ("US", "This role is open to U.S. citizens only.", True),
    ("US", "Applicants must be authorized to work in the U.S. without sponsorship "
           "now or in the future.", True),
    ("GB", "We are unable to provide a Certificate of Sponsorship for this role.", True),
    ("GB", "You must have an existing right to work in the UK.", True),
    ("CA", "Applicants must be legally entitled to work in Canada.", True),
    ("CA", "Canadian citizens or permanent residents only.", True),
    ("DE", "EU work permit required.", True),
    ("SG", "We cannot provide an Employment Pass for this position.", True),
    ("AU", "Applicants must have full working rights in Australia.", True),
    ("", "We do not sponsor visas for this role.", True),
    # Not exclusions. The last two are the measured false positives this scan
    # already had to learn: an application form's own question, and a sentence
    # saying the employer DOES sponsor.
    ("", "Visa sponsorship is available for exceptional candidates.", False),
    ("US", "We have a track record of offering H1B sponsorships.", False),
    ("US", "We will sponsor visas for the right candidate.", False),
    ("", "Do you now or will you in the future require immigration sponsorship?", False),
    ("US", "We are an equal opportunity employer and consider all applicants.", False),
    # A citizenship bar is NOT a sponsorship exclusion — see the credential test.
    ("US", "Must be a U.S. Person as defined by ITAR.", False),
]


@pytest.mark.parametrize("country,sentence,excluded", _SPONSORSHIP_CASES)
def test_the_sponsorship_scan(country, sentence, excluded):
    import jd_extractor

    assert jd_extractor.detect_sponsorship_exclusion(
        sentence, country=country)["excluded"] is excluded


def test_every_market_has_a_worked_example():
    """The gate that makes this scan cheap also made two patterns dead.

    Each sentence is skipped unless it contains one of the market's trigger
    words, so a pattern whose vocabulary is missing from that list can never
    match however correct it is. "must be legally entitled to work in Canada"
    contains no form of the word "sponsor", and the Canadian patterns were
    therefore unreachable the moment they were written — which nothing noticed,
    because an unreachable pattern looks exactly like a posting with nothing to
    find.

    So adding a market means adding a sentence it catches. That is the only
    check that proves the patterns and their triggers agree, and it is cheap to
    satisfy: one line in `_SPONSORSHIP_CASES`.
    """
    import jd_extractor

    covered = {country for country, _sentence, excluded in _SPONSORSHIP_CASES if excluded}
    for country, rules in jd_extractor._SPONSORSHIP_EXCLUSION_BY_COUNTRY.items():
        if rules is jd_extractor._EU_PERMIT and country != "DE":
            # Every EU member shares one entry; DE is the one worked example.
            continue
        assert country in covered, (
            f"{country} has sponsorship patterns but no sentence in "
            f"_SPONSORSHIP_CASES proving one of them fires")


def test_the_neutral_patterns_apply_to_every_market():
    """A market's own wording is added to the country-neutral set, never
    substituted for it."""
    import jd_extractor

    neutral = set(jd_extractor._SPONSORSHIP_EXCLUSION_PATTERNS)
    for country in list(jd_extractor._SPONSORSHIP_EXCLUSION_BY_COUNTRY) + ["", "ZZ"]:
        _triggers, patterns = jd_extractor.sponsorship_rules(country)
        assert neutral <= set(patterns), country


def test_a_dotted_initialism_does_not_split_a_sentence():
    """A plain split on "full stop then whitespace" cut "open to U.S. citizens
    only" in half, and the pattern needing both halves then could not match."""
    import jd_extractor

    assert list(jd_extractor.sentences("Open to U.S. citizens only.")) == \
        ["Open to U.S. citizens only."]
    # but a real sentence boundary still splits
    assert len(list(jd_extractor.sentences(
        "We do not sponsor. Apply anyway."))) == 2


_CREDENTIAL_CASES = [
    ("Must be a U.S. Person as defined by ITAR.", "citizenship or residency status"),
    ("Open to Canadian citizens only.", "citizenship or residency status"),
    ("This role requires an active Secret clearance.", "security clearance"),
    ("A valid driver's license is required.", "driving licence"),
    ("CPA license required and in good standing.", "professional licence"),
]


@pytest.mark.parametrize("sentence,kind", _CREDENTIAL_CASES)
def test_requirements_no_edit_can_satisfy_are_counted_not_judged(sentence, kind):
    """Same reasoning as the sponsorship scan: a clearance or a citizenship bar
    reads as ordinary boilerplate, so a model weighs it as one more preferred
    qualification. It is reported, never used to block — a posting that merely
    prefers a clearance is worded the same as one that demands it, and the
    applicant is the one who knows whether they hold it."""
    import jd_extractor

    assert kind in [c["kind"] for c in jd_extractor.detect_hard_credentials(sentence)]


def test_an_ordinary_posting_sentence_triggers_nothing():
    import jd_extractor

    plain = ("We are hiring a product manager to own the payments roadmap. "
             "You will work with engineering and design.")
    assert jd_extractor.detect_hard_credentials(plain) == []
    assert jd_extractor.detect_sponsorship_exclusion(plain)["excluded"] is False


# ── the axes, one at a time ─────────────────────────────────────────────────

def test_an_unset_axis_screens_nothing():
    """The rule the whole design rests on. A profile that says nothing about
    location, level or pay must not filter on any of them — the opposite is how
    the example profile's role list came to be applied to every posting a new
    user saw."""
    bare = _profile(search={})
    assert _axes(bare, location="Reykjavik, IS", employment_type="contract",
                 work_arrangement="onsite", years_required=15,
                 seniority_level="director",
                 comp_range={"min": 1, "max": 2, "currency": "USD",
                             "period": "year"}) == []


def test_an_unstated_posting_fact_screens_nothing():
    """The other direction: a fully specified profile against a posting that says
    nothing. Unknown is not wrong."""
    prof = _profile(search={"kind": "full_time", "locations": ["London"],
                            "seniority": ["senior"], "work_arrangement": ["hybrid"],
                            "comp_floor": {"amount": 90000, "currency": "GBP"}})
    assert _axes(prof) == []


def test_a_remote_role_is_in_every_location_you_named():
    prof = _profile(search={"locations": ["London"]})
    assert _axes(prof, location="Berlin, DE") != []
    assert _axes(prof, location="Berlin, DE", work_arrangement="remote") == []


def test_relocation_turns_a_location_mismatch_off():
    elsewhere = {"location": "Berlin, DE"}
    assert _axes(_profile(search={"locations": ["London"], "relocate": False}),
                 **elsewhere) != []
    assert _axes(_profile(search={"locations": ["London"], "relocate": True}),
                 **elsewhere) == []


def test_a_location_matches_loosely_in_both_directions():
    """"London" has to match "London, GB", and "San Francisco Bay Area" has to be
    matched by a posting that just says San Francisco."""
    assert _axes(_profile(search={"locations": ["London"]}),
                 location="London, GB") == []
    assert _axes(_profile(search={"locations": ["San Francisco Bay Area"]}),
                 location="San Francisco, CA, US") == []


def test_years_required_is_compared_with_tolerance():
    """Postings round their own requirement up, so one year over is not a
    mismatch. Two is where it starts meaning something."""
    prof = _profile(background={"years_experience": 5})
    assert _axes(prof, years_required=6) == []
    assert _axes(prof, years_required=7) == []
    assert _axes(prof, years_required=8) != []


def test_a_posting_that_contradicts_its_own_years_requirement_is_not_believed():
    """Measured shape: a posting titled for an intern asking for eight years in
    its requirements block, because the block was carried over from another
    template. The degree axis has had this rescue for a while; this is the
    version of it that matters to an experienced-hire search."""
    prof = _profile(background={"years_experience": 2})
    violations = fit_gate.check_hard_axes(
        {"years_required": 8, "role": "Software Engineer Intern",
         "seniority_level": "intern"}, prof)
    assert [v for v in violations if v["axis"] == "years_experience"] == []
    advisory = [v for v in violations if v.get("advisory")]
    assert advisory and "not believed" in advisory[0]["message"]
    # An advisory note is not a mismatch, so it must not move the tier.
    assert fit_gate._clamp("high", "strong", violations)[0] == "high"


def test_pay_is_compared_against_the_top_of_the_range_and_only_in_one_currency():
    prof = _profile(search={"comp_floor": {"amount": 100000, "currency": "USD",
                                           "period": "year"}})
    low = {"min": 60000, "max": 80000, "currency": "USD", "period": "year"}
    high = {"min": 90000, "max": 130000, "currency": "USD", "period": "year"}
    assert _axes(prof, comp_range=low) != []
    assert _axes(prof, comp_range=high) == []
    # A cross-currency comparison would need exchange rates nobody here should
    # be guessing, so it is skipped rather than approximated.
    assert _axes(prof, comp_range={**low, "currency": "JPY"}) == []


def test_an_hourly_range_is_annualised_before_it_is_compared():
    prof = _profile(search={"comp_floor": {"amount": 100000, "currency": "USD",
                                           "period": "year"}})
    assert _axes(prof, comp_range={"min": 80, "max": 90, "currency": "USD",
                                   "period": "hour"}) == []
    assert _axes(prof, comp_range={"min": 14, "max": 15, "currency": "USD",
                                   "period": "hour"}) != []


# ── how violations move the tier ────────────────────────────────────────────

def test_one_mismatch_caps_the_tier_and_two_force_the_bottom():
    one = [{"axis": "location", "message": "wrong city"}]
    two = one + [{"axis": "compensation", "message": "below your floor"}]
    assert fit_gate._clamp("high", "strong", one) == ("mid", "wrong city")
    assert fit_gate._clamp("high", "strong", two)[0] == "low"


def test_a_hard_mismatch_overrides_the_title_floor():
    """"The title matches a role you apply to" is a guess about taste; "this role
    is in a city you said you would not move to" is a fact about the posting."""
    two = [{"axis": "a", "message": "m1"}, {"axis": "b", "message": "m2"}]
    assert fit_gate._clamp("low", "strong", two)[0] == "low"


def test_the_fit_prompt_names_neither_a_search_kind_nor_a_taste():
    """`fit_gate.py` said "screening internship postings" literally, and offered
    shift work, field operations and pure administration as examples of a bad
    posting — one person's taste, shipped to everyone, including people looking
    for exactly the operations roles it down-ranked."""
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(here, "fit_gate.py"), encoding="utf-8") as f:
        body = f.read()
    template = fit_gate.PREFIT_PROMPT_TEMPLATE
    assert "{search_phrase}" in template
    assert "{avoid_clause}" in template
    assert "internship postings for one applicant" not in template
    assert "shift work, field operations" not in template
    # and the taste comes from the profile
    prof = _profile(search={"dealbreakers": ["shift work"], "avoid_roles": ["warehouse"]})
    with _installed(prof):
        clause = fit_gate._avoid_clause()
    assert "shift work" in clause and "warehouse" in clause
    # the file itself no longer carries the hardcoded taste either
    assert "shift work, field operations, pure administration" not in body


@pytest.mark.parametrize("value,expected", [
    ("FullTime", "full_time"), ("Full-time", "full_time"), ("Intern", "internship"),
    ("summer internship", "internship"), ("Contractor", "contract"),
    ("", None), ("???", None),
])
def test_employment_type_arrives_in_one_vocabulary(value, expected):
    """Every source spells it differently, and the fit gate compares it against
    one word. Anything unrecognised becomes None, so the axis is skipped rather
    than screened on a guess."""
    from tracker.sources import base

    assert base.normalise_employment(value) == expected


# ── the four people, screened on years ──────────────────────────────────────

def test_a_new_graduate_is_screened_as_zero_years_not_as_unknown():
    """`years_experience` is left empty for a student or new graduate, which is
    the honest value — and it meant the years check did not run for them at all,
    because it needs a number on both sides. The group most likely to be
    mismatched on experience was the one group with no protection: a posting
    requiring eight years came back with nothing conflicting, ranked alongside
    the graduate schemes."""
    grad = _profile(background={"kind": "recent_graduate", "years_experience": None,
                                "education": {"program": "BSc Computer Science",
                                              "school": "TU Berlin", "completed": True}},
                    search={"kind": "new_grad"})
    assert grad.effective_years == 0
    assert _axes(grad, years_required=8) != []
    # within tolerance, because postings round their own requirement up
    assert _axes(grad, years_required=2) == []


def test_an_experienced_profile_that_says_nothing_is_not_screened_on_years():
    """"I did not say" is different from "none". Only a student or new graduate
    is read as zero."""
    quiet = _profile(background={"kind": "experienced", "years_experience": None},
                     search={"kind": "full_time"})
    assert quiet.effective_years is None
    assert _axes(quiet, years_required=10) == []


def test_a_recent_graduate_is_not_compared_against_current_students():
    """The recruiter persona's comparison set is the whole point of that prompt.
    A graduate was being described as one of the students."""
    grad = _profile(background={"kind": "recent_graduate",
                                "education": {"program": "BSc Computer Science"}})
    student = _profile(background={"kind": "enrolled_student",
                                   "education": {"program": "MBA"}})
    assert grad.peer_pool_phrase == "recent BSc Computer Science graduates from strong programmes"
    assert student.peer_pool_phrase == "MBA students from strong programmes"


def test_an_employment_type_mismatch_is_spelled_the_way_postings_spell_it():
    prof = _profile(search={"kind": "contract"})
    [violation] = fit_gate.check_hard_axes({"employment_type": "full_time"}, prof)
    assert "full-time" in violation["message"]
    assert "full time" not in violation["message"]


def test_an_ai_posting_keeps_its_most_repeated_keyword():
    """Single tokens of two characters or fewer were dropped as a blunt second
    line of defence behind the stopword list. On an AI posting that deletes the
    most important ATS keyword there is: measured on a realistic one, "AI"
    appeared eleven times and "ML" five, and neither reached the list — while
    "building" and "production" did.

    The exemption comes from the posting's own capitalisation rather than from a
    list of known acronyms, so it works for whatever a field calls things."""
    import keyword_extractor as ke

    jd = ("Senior AI Engineer, Applied ML. We build AI products on LLMs. You will own "
          "the AI platform serving our ML models. AI agents, RAG pipelines and LLM "
          "evaluation required. Our AI stack is Python. 5+ years building ML systems. "
          "Deep LLM fine-tuning and RAG experience. AI safety and LLM evaluation. "
          "Work with the AI research team to ship ML features.")
    terms = dict(ke.extract_keywords(jd, top_n=12))
    assert "ai" in terms
    assert "ml" in terms
    assert "llm" in terms
    assert terms["ai"] > terms.get("building", 0)


def test_an_ordinary_posting_admits_no_spurious_short_tokens():
    import keyword_extractor as ke

    plain = ("We need a product manager to own the roadmap. The product manager works "
             "with design. A product manager at our company owns outcomes. ") * 2
    terms = [t for t, _c in ke.extract_keywords(plain, top_n=10)]
    assert all(len(t) > 2 for t in terms if " " not in t)


def test_acronyms_are_read_from_capitalisation_not_a_word_list():
    import keyword_extractor as ke

    assert "ux" in ke.acronyms("We need a UX designer")
    assert "qa" in ke.acronyms("QA owns the release")
    assert "etl" in ke.acronyms("Own the ETL pipeline")
    # ordinary words are not capitalised that way in a posting
    assert "to" not in ke.acronyms("We need someone to own this")
    assert "a" not in ke.acronyms("A designer")
