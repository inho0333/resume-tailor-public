"""The guards that catch a broken setup.

Every test here is a problem that a first-time setup actually hit, where the
tool either corrupted its own configuration or produced a résumé missing most of
its content and reported PASS. They are pinned so that cannot come back.
"""

import os
import sys

import docx
import pytest
import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ask
import config
import errors
import minimal_tailor
import profile as profile_module
import resume_writer
import run

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


# ── profile.yaml copied and never edited ────────────────────────────────────

def test_unedited_profile_is_detected():
    """The example copied verbatim must be reported, not accepted.

    Forgetting to edit it is silent: every prompt then judges postings against a
    fictional person's degree and calls someone else's bullets your voice.
    """
    example = profile_module.load(path=profile_module.EXAMPLE_PATH)
    stale = example.unedited_fields()
    assert "name" in stale and "education.school" in stale
    assert "style_examples" in stale


def test_edited_profile_is_not_flagged():
    data = yaml.safe_load(open(profile_module.EXAMPLE_PATH, encoding="utf-8"))
    data["name"] = "Sam Rivera"
    data["education"]["school"] = "Northwestern Kellogg"
    data["career_summary"] = "Payments product management, then seed investing."
    data["employers"] = ["Meridian Pay", "Harbor Capital"]
    data["style_examples"] = ["Shipped a thing that moved a number by 11%, measured in SQL"]
    data["target_roles"] = ["payments product"]
    assert profile_module.Profile(data).unedited_fields() == []


# ── bootstrap must not eat user_config.yaml ─────────────────────────────────

def test_bootstrap_preserves_existing_user_config(tmp_path, monkeypatch):
    """It patches the two keys it derives and leaves the rest of the file alone.

    It used to rewrite the file from scratch, which deleted base_resume_path,
    tailor_mode and the backend choice — exactly what the setup guide tells you
    to set in the step before bootstrap — along with every comment. The comments
    matter: they are the copied explanations of each setting plus whatever notes
    the user wrote about their own decisions.
    """
    cfg = tmp_path / "user_config.yaml"
    cfg.write_text(
        "# Targeting the corp-dev posting; v3 of the résumé.\n"
        "# Keep the Apple template out of this.\n"
        'resume_path: "/old/path.docx"\n'
        "base_resume_path: /mine/base.docx\n"
        "tailor_mode: minimal\n"
        "light_backend: ollama\n"
        "digest_hours: [7, 19]\n"
        "\n"
        "# counts came off the printed résumé\n"
        "max_bullets_per_section:\n"
        "  OLD CORP.: 9\n",
        encoding="utf-8")
    monkeypatch.setattr(config, "USER_CONFIG_PATH", str(cfg))

    sections = [{"name": "ACME CORP.", "header_text": "ACME CORP., Austin, TX",
                 "bullets": [{"id": "acme-b1", "text": "a"}, {"id": "acme-b2", "text": "b"}]}]
    run._write_user_config("/new/resume.docx", sections)

    text = cfg.read_text(encoding="utf-8")
    written = yaml.safe_load(text)
    assert written["resume_path"] == "/new/resume.docx"
    assert written["max_bullets_per_section"] == {"ACME CORP.": 2}
    assert written["base_resume_path"] == "/mine/base.docx"
    assert written["light_backend"] == "ollama"
    assert written["digest_hours"] == [7, 19]
    # the prose survives, which a load-and-dump would have silently dropped
    assert "# Targeting the corp-dev posting; v3 of the résumé." in text
    assert "# Keep the Apple template out of this." in text
    assert "# counts came off the printed résumé" in text
    assert (tmp_path / "user_config.yaml.bak").exists()


def test_patch_yaml_keys_ignores_a_commented_out_key(tmp_path):
    """`# resume_path: ...` is documentation, not a setting."""
    patched = run._patch_yaml_keys(
        "# resume_path: /not/this/one.docx\nresume_path: /yes.docx\n",
        {"resume_path": ["resume_path: /new.docx"]})
    assert "# resume_path: /not/this/one.docx" in patched
    assert yaml.safe_load(patched)["resume_path"] == "/new.docx"


def test_bootstrap_first_run_makes_minimal_mode_work(tmp_path, monkeypatch):
    """With no config yet, bootstrap points minimal mode at the same document.

    Otherwise the default mode fails on the very next command with
    "base_resume_path not set", which is not a useful first experience.
    """
    cfg = tmp_path / "user_config.yaml"
    monkeypatch.setattr(config, "USER_CONFIG_PATH", str(cfg))
    run._write_user_config("/new/resume.docx", [{"name": "A", "header_text": "A",
                                                 "bullets": [{"id": "a-b1", "text": "a"}]}])
    written = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    assert written["base_resume_path"] == "/new/resume.docx"
    assert written["tailor_mode"] == "minimal"


# ── bootstrap writes base_resume.yaml ───────────────────────────────────────

def test_bootstrap_writes_a_loadable_base_resume(tmp_path, monkeypatch):
    """Minimal mode cannot run without this file, and hand-writing it (every
    header_text matching a .docx paragraph character for character) was the main
    source of "header not found"."""
    base_path = tmp_path / "base_resume.yaml"
    monkeypatch.setattr(minimal_tailor, "BASE_YAML_PATH", str(base_path))
    sections = [
        {"name": "ACME CORP.", "header_text": "ACME CORP. (Acme Group), Austin, TX",
         "bullets": [{"id": "acme-b1", "text": "Did a thing, measured 18%."}]},
        {"name": "ADDITIONAL", "header_text": "ADDITIONAL",
         "bullets": [{"id": "additional-b1", "text": "Skills: SQL, Looker"}]},
    ]
    assert run._write_base_resume_draft("/path/to/resume.docx", sections) is True

    base = minimal_tailor.load_base(str(base_path))
    assert base["source_docx"] == "/path/to/resume.docx"
    assert [s["header_text"] for s in base["sections"]] == [s["header_text"] for s in sections]
    assert set(minimal_tailor.base_index(base)) == {"acme-b1", "additional-b1"}


def test_bootstrap_never_overwrites_an_existing_base(tmp_path, monkeypatch):
    """That file holds decisions about which bullets are approved."""
    base_path = tmp_path / "base_resume.yaml"
    base_path.write_text("mine: true\n", encoding="utf-8")
    monkeypatch.setattr(minimal_tailor, "BASE_YAML_PATH", str(base_path))
    assert run._write_base_resume_draft("/x.docx", []) is False
    assert base_path.read_text(encoding="utf-8") == "mine: true\n"


# ── base_resume.yaml is checked against the DB and the document ─────────────

def _write_docx(path, headers_and_bullets):
    """A .docx shaped like a résumé: heading paragraph, then List Bullet lines."""
    doc = docx.Document()
    doc.add_paragraph("EXPERIENCE")
    for header, bullets in headers_and_bullets:
        doc.add_paragraph(header)
        for text in bullets:
            doc.add_paragraph(text, style="List Bullet")
    doc.save(str(path))
    return str(path)


@pytest.fixture
def setup_files(tmp_path, monkeypatch):
    """A consistent db + base + .docx trio, which individual tests then break."""
    docx_path = _write_docx(tmp_path / "resume.docx", [
        ("ACME CORP. (Acme Group), Austin, TX", ["Did a thing, 18%.", "Did another, 4,000."]),
        ("ADDITIONAL", ["Skills: SQL, Looker"]),
    ])
    db = {"sections": [
        {"name": "ACME CORP.", "header_text": "ACME CORP. (Acme Group), Austin, TX", "bullets": [
            {"id": "acme-b1", "text": "Did a thing, 18%.", "tags": {}},
            {"id": "acme-b2", "text": "Did another, 4,000.", "tags": {}}]},
        {"name": "ADDITIONAL", "header_text": "ADDITIONAL", "bullets": [
            {"id": "additional-b1", "text": "Skills: SQL, Looker", "tags": {}}]},
    ]}
    db_path = tmp_path / "resume_db.yaml"
    db_path.write_text(yaml.safe_dump(db, allow_unicode=True, sort_keys=False), encoding="utf-8")

    base = {"source_docx": docx_path, "sections": [
        {"name": s["name"], "header_text": s["header_text"],
         "bullets": [{"id": b["id"], "text": b["text"]} for b in s["bullets"]]}
        for s in db["sections"]]}
    base_path = tmp_path / "base_resume.yaml"

    def write_base(data):
        base_path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
                             encoding="utf-8")
        return str(base_path)

    monkeypatch.setattr(config, "DB_PATH", str(db_path))
    monkeypatch.setattr(config, "BASE_RESUME_PATH", docx_path)
    monkeypatch.setattr(config, "TAILOR_MODE", "minimal")
    write_base(base)
    return {"docx": docx_path, "db": db, "base": base, "base_path": str(base_path),
            "write_base": write_base}


def _statuses(rows):
    return {status for status, _, _ in rows}


def test_consistent_base_passes(setup_files):
    rows = run._check_base_resume(setup_files["base_path"])
    assert _statuses(rows) == {run._OK}


def test_header_text_mismatch_is_fatal(setup_files):
    """The one-character mismatch that used to surface only at render time."""
    base = setup_files["base"]
    base["sections"][0]["header_text"] = "ACME CORP."
    rows = run._check_base_resume(setup_files["write_base"](base))
    assert run._FAIL in _statuses(rows)
    detail = " ".join(d for _, d, _ in rows)
    assert "header_text" in detail and "ACME CORP. (Acme Group), Austin, TX" in detail


def test_unknown_bullet_id_is_fatal(setup_files):
    base = setup_files["base"]
    base["sections"][0]["bullets"][0]["id"] = "acme-typo"
    rows = run._check_base_resume(setup_files["write_base"](base))
    assert run._FAIL in _statuses(rows)
    assert any("not in resume_db.yaml" in d for _, d, _ in rows)


def test_section_missing_from_the_base_is_fatal(setup_files):
    """A section the base never mentions renders nothing: that is a broken file,
    not an instruction to drop a job from the résumé."""
    base = setup_files["base"]
    base["sections"] = base["sections"][:1]
    rows = run._check_base_resume(setup_files["write_base"](base))
    assert run._FAIL in _statuses(rows)
    assert any("ADDITIONAL" in d for _, d, _ in rows)


def test_partial_section_is_a_warning_not_an_error(setup_files):
    """Dropping a bullet from the approved set is a legitimate choice — but you
    are told, because the tailored résumé will be shorter than the document."""
    base = setup_files["base"]
    base["sections"][0]["bullets"] = base["sections"][0]["bullets"][:1]
    rows = run._check_base_resume(setup_files["write_base"](base))
    assert run._FAIL not in _statuses(rows)
    assert any("lists 1 of the 2 bullets" in d for _, d, _ in rows)


def test_text_drift_against_the_db_is_a_warning(setup_files):
    base = setup_files["base"]
    base["sections"][0]["bullets"][0]["text"] = "Did a thing, 19%."
    rows = run._check_base_resume(setup_files["write_base"](base))
    assert any("differ from resume_db.yaml" in d for _, d, _ in rows)


def test_validate_inputs_refuses_before_any_model_call(setup_files, monkeypatch):
    """A broken base must stop the run at once. It used to be found after the
    posting had been fetched and classified — two model calls already spent."""
    base = setup_files["base"]
    base["sections"] = base["sections"][:1]
    monkeypatch.setattr(minimal_tailor, "BASE_YAML_PATH",
                        setup_files["write_base"](base))
    with pytest.raises(errors.SetupError) as excinfo:
        run._validate_inputs(setup_files["docx"])
    assert "base_resume.yaml" in str(excinfo.value)


# ── the render path may not lose content ────────────────────────────────────

def test_render_counts_must_match_the_selection():
    db_sections = [{"name": "ACME CORP.", "header_text": "ACME CORP.",
                    "bullets": [{"id": "acme-b1", "text": "a", "tags": {}}]}]
    result = {"sections": {"ACME CORP.": [{"id": "acme-b1", "text": "a"},
                                          {"id": "acme-b2", "text": "b"}]}}
    # what we meant to write, and what the document ended up with
    run._verify_render_counts(result, db_sections, {"ACME CORP.": 2})
    with pytest.raises(errors.SetupError) as excinfo:
        run._verify_render_counts(result, db_sections, {"ACME CORP.": 1})
    assert "does not match what was selected" in str(excinfo.value)


def test_minimal_mode_will_not_substitute_a_database_bullet(tmp_path):
    """In minimal mode an uncovered section means base_resume.yaml is wrong.

    Substituting "the first bullet in the DB" is how a résumé came out with four
    of its eight bullets while every check reported PASS.
    """
    path = _write_docx(tmp_path / "r.docx", [("ACME CORP.", ["one"]), ("ADDITIONAL", ["two"])])
    db_sections = [
        {"name": "ACME CORP.", "header_text": "ACME CORP.",
         "bullets": [{"id": "acme-b1", "text": "one", "tags": {}}]},
        {"name": "ADDITIONAL", "header_text": "ADDITIONAL",
         "bullets": [{"id": "additional-b1", "text": "two", "tags": {}}]},
    ]
    result = {"sections": {"ACME CORP.": [{"id": "acme-b1", "text": "one edited"}]}}

    with pytest.raises(errors.SetupError) as excinfo:
        resume_writer.render_sections(docx.Document(path), result, db_sections,
                                      allow_db_fallback=False)
    assert "ADDITIONAL" in str(excinfo.value)

    # rebuild mode keeps the old forgiving behaviour
    applied = resume_writer.render_sections(docx.Document(path), result, db_sections,
                                            allow_db_fallback=True)
    assert applied["ADDITIONAL"] == ["additional-b1"]


# ── prompts must not explode without a terminal ─────────────────────────────

def test_confirm_returns_its_default_without_a_terminal(capsys):
    """A piped or scheduled run used to die with EOFError at the confirmation —
    after the expensive calls had been made."""
    assert ask.confirm("go? ", default=True) is True
    assert ask.confirm("go? ", default=False) is False
    assert "no terminal" in capsys.readouterr().out


def test_line_returns_its_default_without_a_terminal():
    assert ask.line("answer: ") == ""
    assert ask.line("answer: ", default="skip") == "skip"
