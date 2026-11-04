"""pagefit's widow detection and compression guardrails, plus the ADDITIONAL category prefix.

The real page measurement (Word, driven over osascript) is deliberately NOT exercised here:
the suite has to run where Word does not exist, and the measurement itself is checked by hand
with `pagefit.py --json`. What is pinned here is the pure logic that takes a measurement and
decides what to fix, and the refusal to accept a compression that changes a fact.
"""

import pagefit
import tailor_engine


def _bullet(text, lines, last_line):
    return {
        "index": 1, "lines": lines, "last_line": last_line,
        "last_line_chars": len(last_line),
        "last_line_words": len(last_line.split()),
        "text": text,
    }


# ── widow detection ───────────────────────────────────────────────────────────

def test_one_word_last_line_is_a_widow():
    m = {"bullets": [_bullet("A" * 230, 3, "access")]}
    assert len(pagefit.find_widows(m)) == 1


def test_full_last_line_is_not_a_widow():
    """A bullet whose last line is full would need more than 100 characters cut to lose a
    line, so it is not a candidate."""
    m = {"bullets": [_bullet("A" * 246, 3, "and rebuilding refund adjustments with engineering")]}
    assert pagefit.find_widows(m) == []


def test_single_line_bullet_is_never_a_widow():
    m = {"bullets": [_bullet("Volunteering: Mentored 50+ founders", 1, "founders")]}
    assert pagefit.find_widows(m) == []


def test_skills_line_is_protected():
    """The Skills/Languages line has a fixed format, so automatic compression skips it."""
    m = {"bullets": [_bullet("Skills: Data Analysis (SQL, Python) | Languages: Korean", 2,
                             "(Fluent)")]}
    assert pagefit.find_widows(m) == []
    assert pagefit.is_protected("Skills: Data Analysis")


def test_widows_sorted_by_waste():
    m = {"bullets": [
        dict(_bullet("x" * 230, 3, "two words"), index=2),
        dict(_bullet("y" * 230, 3, "one"), index=3),
    ]}
    assert [w["index"] for w in pagefit.find_widows(m)] == [3, 2]


def test_trim_target_includes_safety_margin():
    b = _bullet("z" * 230, 3, "access")
    assert pagefit.trim_target_chars(b) == len("access") + pagefit.TRIM_SAFETY_CHARS


# ── compression guardrails ────────────────────────────────────────────────────

class _Backend:
    def __init__(self, reply):
        self.reply = reply
        self.name = "fake"

    def complete(self, prompt):
        return self.reply


ORIGINAL = ("Established a SQL-based KPI framework through a 10x volume surge and presented it "
            "company-wide each month, after cohort analysis showed novelty goods grew volume "
            "but rarely repeated, shifting sourcing to hold repurchase above 30%")


def test_shorten_accepts_a_shorter_faithful_rewrite():
    short = ("Established a SQL-based KPI framework through a 10x volume surge, presented "
             "company-wide monthly, after cohort analysis showed novelty goods grew volume "
             "but rarely repeated, moving sourcing above 30% repurchase")
    out = tailor_engine.shorten_bullet(_Backend(short), ORIGINAL, 16)
    assert out == short


def test_shorten_rejects_invented_numbers():
    """A figure that is not in the original is rejected — the code stops a compression from
    changing a fact on the way through."""
    bad = ("Established a SQL-based KPI framework through a 12x volume surge, presented "
           "company-wide monthly, sourcing shifted to hold repurchase above 30%")
    assert tailor_engine.shorten_bullet(_Backend(bad), ORIGINAL, 16) is None


def test_shorten_rejects_em_dash():
    bad = ("Established a SQL-based KPI framework through a 10x volume surge — presented "
           "company-wide monthly, holding repurchase above 30%")
    assert tailor_engine.shorten_bullet(_Backend(bad), ORIGINAL, 16) is None


def test_shorten_rejects_insufficient_trim():
    barely = ORIGINAL[:-3]
    assert tailor_engine.shorten_bullet(_Backend(barely), ORIGINAL, 30) is None


def test_shorten_allows_dropping_a_number():
    """**Dropping** a figure is allowed: that is a loss of information, not a falsehood."""
    dropped = ("Established a SQL-based KPI framework through a volume surge, presented "
               "company-wide monthly, after cohort analysis showed novelty goods grew volume "
               "but rarely repeated, shifting sourcing")
    assert tailor_engine.shorten_bullet(_Backend(dropped), ORIGINAL, 20) == dropped


# ── the ADDITIONAL category prefix ────────────────────────────────────────────

DB_SECTIONS = [{
    "name": "ADDITIONAL", "header_text": "ADDITIONAL",
    "bullets": [
        {"id": "add-product-building", "text": "...", "tags": {}, "label": "Product Building"},
        {"id": "add-ai-services", "text": "...", "tags": {}, "label": "AI Services"},
        {"id": "add-venture", "text": "...", "tags": {}, "label": "Entrepreneurship"},
        {"id": "example-kpi", "text": "...", "tags": {}},
    ],
}]


def test_label_is_prepended_when_missing():
    sections = {"ADDITIONAL": [{"id": "add-product-building",
                                "text": "Prototyped a payments flow"}]}
    out = tailor_engine.apply_section_labels(sections, DB_SECTIONS)
    assert out["ADDITIONAL"][0]["text"] == "Product Building: Prototyped a payments flow"


def test_existing_label_is_not_duplicated():
    sections = {"ADDITIONAL": [{"id": "add-ai-services",
                                "text": "AI Services: Developed two bots"}]}
    out = tailor_engine.apply_section_labels(sections, DB_SECTIONS)
    assert out["ADDITIONAL"][0]["text"] == "AI Services: Developed two bots"


def test_wrong_label_is_replaced_with_the_db_one():
    sections = {"ADDITIONAL": [{"id": "add-venture",
                                "text": "Product Building: Co-founded a resale marketplace"}]}
    out = tailor_engine.apply_section_labels(sections, DB_SECTIONS)
    assert out["ADDITIONAL"][0]["text"] == "Entrepreneurship: Co-founded a resale marketplace"


def test_bullets_without_label_are_untouched():
    sections = {"ADDITIONAL": [{"id": "example-kpi", "text": "Established a KPI framework"}]}
    out = tailor_engine.apply_section_labels(sections, DB_SECTIONS)
    assert out["ADDITIONAL"][0]["text"] == "Established a KPI framework"


def test_colon_inside_the_sentence_is_not_mistaken_for_a_label():
    sections = {"ADDITIONAL": [{
        "id": "add-product-building",
        "text": "Built a scheduling tool end to end: four subsystems, written alone",
    }]}
    out = tailor_engine.apply_section_labels(sections, DB_SECTIONS)
    assert out["ADDITIONAL"][0]["text"].startswith("Product Building: Built a scheduling tool")
    assert out["ADDITIONAL"][0]["text"].count("Product Building") == 1
