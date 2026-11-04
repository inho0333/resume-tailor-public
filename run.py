"""
Resume Tailor CLI

Usage:
    python run.py tailor --url "<JD URL>" --company "Acme" --role "Summer Associate" [--resume path.docx]
    python run.py tailor --jd-file jd.txt --company "Acme" --role "Summer Associate"
"""

import argparse
import datetime
import json
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import docx
import yaml

import ask
import bootstrap_engine
import company_research
import config
import confirm_state
import decisions
import errors
import fit_gate
import genlock
import jd_extractor
import keyword_extractor
import llm_client
import minimal_tailor
import pagefit
import profile as profile_module
import recruiter_review
import resume_db
import resume_parser
import resume_writer
import tailor_engine
import validate


def _get_jd_text(args):
    if args.jd_file:
        with open(args.jd_file, "r", encoding="utf-8") as f:
            return f.read()

    if args.url:
        print(f"Fetching the posting page: {args.url}")
        text = jd_extractor.fetch_jd_text_from_url(args.url)
        if text:
            return text
        if getattr(args, "auto", False):
            raise RuntimeError("Could not extract the JD body from the URL (likely a "
                               "JS-rendered or login-walled page) — auto mode cannot fall "
                               "back to pasting")
        print("Could not extract enough JD text from the URL "
              "(the site may require login or render with JS).")

    if getattr(args, "auto", False):
        raise RuntimeError("auto mode needs --jd-file or --url (there is no stdin to paste into)")

    print("Paste the JD text here. When you are done, press Ctrl+D (Linux/Mac) or "
          "Ctrl+Z (Windows) on an empty line.")
    pasted = sys.stdin.read()
    if not pasted.strip():
        print("The JD is empty. Exiting.")
        sys.exit(1)
    return pasted


def _clamp_to_caps(sections_dict, caps):
    """Trim each section to config.MAX_BULLETS_PER_SECTION and warn when we had to.
    We do not retry: a human reviews every draft anyway (notify.Report), so the cost of
    retry logic is not worth it for this failure mode."""
    for name, items in sections_dict.items():
        cap = caps.get(name)
        if cap and len(items) > cap:
            print(f"  [warning] section '{name}' exceeded the cap of {cap} "
                  f"({len(items)} bullets) — trimmed to {cap}")
            sections_dict[name] = items[:cap]
    return sections_dict


def _safe_name(s):
    return "".join(c for c in s if c.isalnum() or c in " _-").strip().replace(" ", "")


def _company_output_dir(company):
    """One subfolder per company. The code used to dump everything flat into output/ and
    the user kept sorting the files into per-company folders by hand — so the code now
    writes new runs where they were being filed anyway."""
    d = os.path.join(config.OUTPUT_DIR, _safe_name(company) or "Unknown")
    os.makedirs(d, exist_ok=True)
    return d


def _find_output_file(company, filename):
    """Prefer the per-company subfolder; fall back to the old flat path so output from
    runs made before the subfolder layout is still found."""
    nested = os.path.join(config.OUTPUT_DIR, _safe_name(company) or "Unknown", filename)
    if os.path.exists(nested):
        return nested
    flat = os.path.join(config.OUTPUT_DIR, filename)
    return flat if os.path.exists(flat) else nested


def _copy_to_sync_dir(company, out_path):
    """Copy the draft into user_config.yaml's `sync_dir` (a cloud-drive folder, say).

    Optional convenience. A mounted drive folder needs no API or auth — a copy is
    enough. Unset, or a folder that is not mounted, means the copy is skipped
    quietly: this must never block the run that produced the draft.
    """
    sync_dir = config.SYNC_DIR
    if not sync_dir:
        return
    if not os.path.isdir(sync_dir):
        print(f"  [sync skipped] folder not found: {sync_dir}")
        return
    try:
        dest_dir = os.path.join(sync_dir, _safe_name(company) or "Unknown")
        os.makedirs(dest_dir, exist_ok=True)
        shutil.copy2(out_path, os.path.join(dest_dir, os.path.basename(out_path)))
        print(f"  copied to sync folder: {dest_dir}")
    except OSError as e:
        print(f"  [warning] copy to sync folder failed (skipped): {e}")


def _enforce_one_page(out_path, resume_path, shorten_backend, report=None):
    """**The one-page guarantee (on by default).** Measure the real page count with Word;
    if the draft spills past one page, or past the line count of the base template, compress
    the widow bullets (the ones whose last line holds only a word or two and so waste a whole
    line) with the heavy backend and measure again. See pagefit.py for the details.

    Why this step exists: validate.py's arithmetic estimate cannot call pass/fail at the
    boundary. One generated draft was scored "+0.0 lines vs. template, expected to stay on
    one page: PASS" and Word measured two pages. Where Word is unavailable this is skipped
    quietly and only the estimate remains."""
    if not pagefit.word_available():
        print("  (no Word — skipping the real page measurement, using validate.py's estimate)")
        return None

    print("\nMeasuring the page count (Microsoft Word)...")

    def shorten(text, target_chars, bullet):
        if pagefit.is_protected(text):
            return None
        return tailor_engine.shorten_bullet(shorten_backend, text, target_chars, bullet)

    state = pagefit.enforce(out_path, baseline_path=resume_path,
                            shorten_fn=shorten if shorten_backend else None)
    print(pagefit.format_report(state))
    if report and state.get("available"):
        report.set_pagefit(state["level"], state["text"])
    return state


def _finalize_and_send(doc, result, db_sections, company, role, resume_path, jd_text, jd_profile,
                       report, shorten_backend=None):
    """Render → save → measure/fix the page count → copy to the sync folder → format checks →
    fill in the report fields (when a report was passed). Shared by `cmd_tailor`'s
    generate-now path and `confirm_handler.handle_make()` (generate after approval). It does
    not call report.send(): each caller sends on its own schedule. Returns out_path.

    shorten_backend: the LLM backend used to compress widow bullets when the draft runs long.
    None means measure only, with no automatic compression.
    """
    if config.TAILOR_MODE == "minimal":
        # In minimal mode **the base resume decides** how many bullets a section has. The
        # per-section cap in user_config belongs to the old rebuild mode, which picked a
        # variable number of bullets out of the DB; a cap smaller than the base silently
        # drops the trailing bullets. Measured once: a cap of 4 on one section made the
        # base's fifth bullet (example-review-ui) disappear.
        pass
    else:
        result["sections"] = _clamp_to_caps(result["sections"], config.MAX_BULLETS_PER_SECTION)

    minimal = config.TAILOR_MODE == "minimal"
    applied = resume_writer.render_sections(doc, result, db_sections,
                                            allow_db_fallback=not minimal)

    _verify_rendered(doc, result, db_sections)
    _verify_render_counts(result, db_sections, _bullet_counts(doc, db_sections))

    out_dir = _company_output_dir(company)
    date_str = datetime.date.today().strftime("%Y%m%d")
    safe_company = _safe_name(company)
    safe_role = _safe_name(role)
    out_name = f"Resume_{safe_company}_{safe_role}_{date_str}.docx"
    out_path = os.path.join(out_dir, out_name)
    resume_writer.save(doc, out_path)
    if report:
        report.set_draft(out_path, applied)
        report.set_minimal_edit(result.get("minimal_edit"))

    # The page fix can rewrite bullets, so it runs **before** the copy to the sync folder.
    # When the checks ran after the copy, an over-long draft went out to the sync folder and
    # to Telegram as-is.
    if config.TAILOR_MODE == "minimal":
        # Here the LLM only deletes words from widow lines; it never rewrites a sentence.
        pagefit_state = _enforce_one_page_minimal(out_path, resume_path, result, db_sections,
                                                  report=report,
                                                  shorten_backend=shorten_backend)
    else:
        pagefit_state = _enforce_one_page(out_path, resume_path, shorten_backend, report)

    _copy_to_sync_dir(company, out_path)

    print("\nRunning format checks (em dashes / fonts / character counts / page headroom)...")
    validation = validate.validate_docx(out_path, baseline_path=resume_path)
    # A real measurement (pagefit) always beats the estimate — the estimate is wrong at
    # the boundary.
    if report and not (pagefit_state and pagefit_state.get("available")):
        level, text = validation["pagefit"]["level"], validation["pagefit"]["text"]
        if pagefit_state and pagefit_state.get("attempted"):
            # Word is installed but the measurement itself failed. This used to fall back to
            # the estimate silently, so a number we had just decided not to trust reached the
            # user as "expected to fit on one page" (see the cases in pagefit.py: estimate
            # PASS, Word measured two pages). Say in the report that this is an estimate.
            level = "tight" if level == "ok" else level
            text = f"{text} / ⚠️ Word measurement failed (estimate)"
        report.set_pagefit(level, text)

    profile_path = os.path.join(out_dir, f"{safe_company}_{safe_role}_jd_profile.json")
    with open(profile_path, "w", encoding="utf-8") as f:
        json.dump(jd_profile, f, ensure_ascii=False, indent=2)

    # Keep the selection/rewrite result itself, so `run.py review` (the after-the-fact
    # recruiter pass) can read bullet ids directly instead of reverse-matching them out of
    # the docx — and so what shipped, and what was cut, stays auditable.
    result_path = os.path.join(out_dir, f"{safe_company}_{safe_role}_result.json")
    with open(result_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    # The full recruiter review (scores, what tripped, before/after per bullet) only gets two
    # lines in the report, so save the whole thing where the reasoning can be checked.
    if result.get("recruiter_review"):
        recruiter_review.save_audit(out_dir, company, role, result["recruiter_review"], _safe_name)

    # Keep the raw jd_text so the polish command can recompute the same keyword frequencies.
    # jd_profile.json is an LLM summary, so the repetition counts from the original are gone.
    jd_text_path = os.path.join(out_dir, f"{safe_company}_{safe_role}_jd_text.txt")
    with open(jd_text_path, "w", encoding="utf-8") as f:
        f.write(jd_text)

    total_bullets = sum(len(ids) for ids in applied.values())
    print(f"\nDone: {out_path}")
    print(f"  bullets selected: {total_bullets} in total")
    for section_name, ids in applied.items():
        print(f"    {section_name}: {len(ids)} {ids}")
    print(f"  JD analysis saved: {profile_path}")
    gaps = result.get("gaps") or []
    if gaps:
        print("\n  gaps still open (unanswered — fill these in by hand):")
        for g in gaps:
            print(f"    - {g.get('item')}")
    return out_path


def _verify_rendered(doc, result, db_sections):
    """Confirm the rendered paragraphs really are the sentences we asked for; raise if not.

    The incident this guards against: in one base resume the bullets used the `List Bullet`
    style (no paragraph numPr), so `find_section_block` could not find the ACME CORP. /
    EXAMPLE CO. blocks and overwrote the ADDITIONAL block instead. The output was
    character-for-character identical to the base resume, and both the page measurement and
    the format checks reported PASS — nothing anywhere told us that every edit and
    replacement had vanished. Now it stops here."""
    rendered = {p.text.strip() for p in doc.paragraphs if p.text.strip()}
    missing = []
    for section in db_sections:
        for item in result.get("sections", {}).get(section["name"], []):
            if item["text"].strip() not in rendered:
                missing.append(f"{section['name']}/{item['id']}")
    if missing:
        raise errors.SetupError(
            "render check failed — these bullets are not in the document: " +
            ", ".join(missing) +
            " (check the section headers and bullet formatting in your template)")


def _bullet_counts(doc, db_sections):
    """{section name: number of bullet paragraphs} as the document stands."""
    counts = {}
    headers = {s["header_text"] for s in db_sections}
    for section in db_sections:
        try:
            _, bullets = resume_writer.find_section_block(
                doc, section["header_text"], stop_texts=headers)
        except ValueError:
            continue
        counts[section["name"]] = len(bullets)
    return counts


def _verify_render_counts(result, db_sections, after):
    """The document must hold exactly as many bullets per section as we decided to render.

    `_verify_rendered` proves each chosen sentence is somewhere in the document;
    this proves nothing extra was left behind and nothing went missing. The
    failure it exists for: a base_resume.yaml covering 2 of a document's 8
    bullets produced a four-bullet résumé, and every check still printed PASS —
    the page measurement even reported the output as six lines shorter than the
    baseline and called that fine.
    """
    wrong = {}
    for section in db_sections:
        name = section["name"]
        expected = len(result.get("sections", {}).get(name, []))
        if not expected:
            continue
        got = after.get(name)
        if got is not None and got != expected:
            wrong[name] = (expected, got)
    if not wrong:
        return
    detail = ", ".join(f"{name}: meant to write {want}, document has {got}"
                       for name, (want, got) in sorted(wrong.items()))
    raise errors.SetupError(
        f"the rendered document does not match what was selected ({detail}). This is usually "
        "a section header or bullet formatting mismatch between your template and "
        "base_resume.yaml / resume_db.yaml; `python run.py doctor` checks both.")


def _enforce_one_page_minimal(out_path, base_docx_path, result, db_sections, report=None,
                              shorten_backend=None, max_rounds=3):
    """The one-page guarantee for minimal mode: **revert edits instead of compressing text**.

    `_enforce_one_page` (rebuild mode) compressed widow bullets with the heavy model, and that
    compression step was exactly what produced sentences you could no longer parse. In minimal
    mode the base resume already fits on one page, so if the draft spills we revert the edit
    that grew the most, biggest first. Worst case we end up with the base resume verbatim,
    and that is one page by construction.

    Returns a state dict shaped like `_enforce_one_page`'s, for the report to render."""
    if not pagefit.word_available():
        print("  (no Word — skipping the real page measurement, using validate.py's estimate)")
        return None

    print("\nMeasuring the page count (Microsoft Word; minimal mode reverts, never compresses)...")
    base = minimal_tailor.load_base()
    try:
        baseline_lines = pagefit.measure(base_docx_path, with_bullets=False)["lines"]
    except pagefit.WordUnavailable as e:
        print(f"  [measurement failed] {e}")
        return {"available": False, "attempted": True}

    for _round in range(max_rounds + 1):
        try:
            measurement = pagefit.measure(out_path, with_bullets=False)
        except pagefit.WordUnavailable as e:
            print(f"  [measurement failed] {e}")
            return {"available": False, "attempted": True}
        pages, lines = measurement["pages"], measurement["lines"]
        print(f"Word measured: {pages} page(s) / {lines} lines; base resume {baseline_lines} lines")
        if pages <= 1 and lines <= baseline_lines:
            # Even at one page, **get rid of the wasted widow lines**. This path only deletes
            # words, so no sentence is rewritten (see minimal_tailor.remove_widows).
            if minimal_tailor.remove_widows(out_path, shorten_backend, base, db_sections):
                final = pagefit.measure(out_path, with_bullets=False)
                pages, lines = final["pages"], final["lines"]
                print(f"Word re-measured: {pages} page(s) / {lines} lines")
            state = {"available": True, "attempted": True, "pages": pages, "lines": lines,
                     "baseline_lines": baseline_lines}
            if report:
                report.set_pagefit("ok", f"measured {pages} page(s) / {lines} lines "
                                         f"(base {baseline_lines} lines)")
            return state
        if not minimal_tailor.revert_largest(result, base):
            break
        doc = docx.Document(base_docx_path)
        # Minimal mode throughout: a section the result does not cover is an error,
        # never a substituted database bullet (see render_sections).
        resume_writer.render_sections(doc, result, db_sections, allow_db_fallback=False)
        resume_writer.save(doc, out_path)

    state = {"available": True, "attempted": True, "pages": pages, "lines": lines,
             "baseline_lines": baseline_lines, "overflow": True}
    if report:
        report.set_pagefit("over", f"⚠️ measured {pages} page(s) / {lines} lines — over the "
                                   f"base resume ({baseline_lines} lines), needs a human look")
    return state


def _build_sections(heavy_backend, jd_profile, db_sections, keyword_counts, chars_budget,
                    extra_facts=None, keep_ids=(), log=print):
    """Build the tailored result (sections_result). `config.TAILOR_MODE` picks the method.

    "minimal" (the default): keep the sentences of the base resume (base_resume.yaml), adjust
    wording only, and swap at most two bullets. One heavy-backend call.
    "rebuild" (the older way): re-select from the DB and rewrite everything. Two calls.

    Returns (sections_result, base_or_None). When base is present, the base resume is what
    sets both the render template and the length budget.
    """
    if config.TAILOR_MODE == "minimal":
        base = minimal_tailor.load_base()
        log("\nMinimal edit against the base resume's sentences (heavy backend, 1 call)...")
        result, _applied = minimal_tailor.run(heavy_backend, base, db_sections, jd_profile,
                                              keyword_counts=keyword_counts,
                                              keep_ids=keep_ids, log=log)
        return result, base

    log("\nSelecting and rewriting resume bullets (heavy backend, 2 stages)...")
    result = tailor_engine.generate_section_revisions(
        heavy_backend, jd_profile, db_sections, keyword_counts=keyword_counts,
        extra_facts=extra_facts, max_per_section=config.MAX_BULLETS_PER_SECTION,
        total_chars_budget=chars_budget, log=log)
    return result, None


def _resume_template_path(fallback):
    """The .docx to render into. In minimal mode the base resume (BASE_RESUME_PATH) is the
    reference for both formatting and sentences."""
    if config.TAILOR_MODE == "minimal" and config.BASE_RESUME_PATH:
        return config.BASE_RESUME_PATH
    return fallback


def _build_sections_budget(doc, db_sections):
    """Length budget. In minimal mode the cap is simply the base resume's total bullet
    character count."""
    if config.TAILOR_MODE == "minimal":
        try:
            return minimal_tailor.base_total_chars(minimal_tailor.load_base())
        except (OSError, ValueError) as e:
            print(f"  [warning] could not compute the base resume's length budget (skipped): {e}")
            return None
    return _template_chars_budget(doc, db_sections)


def _validate_inputs(resume_path):
    """Check that the resume template and the experience DB are actually usable, before we
    enter any expensive step. Sending an approval request and only failing with "file not
    found" after the user presses the button wastes their time twice."""
    if not resume_path:
        raise errors.SetupError(
            "no resume template configured — if this is your first run, start with "
            "'python run.py bootstrap --resume <your_resume.docx>' "
            "(or pass --resume on every run)."
        )
    if not os.path.exists(resume_path):
        raise errors.SetupError(f"resume file not found: {resume_path}")
    if not os.path.exists(config.DB_PATH):
        raise errors.SetupError(
            f"no experience DB at {config.DB_PATH} — run "
            "'python run.py bootstrap --resume <your_resume.docx>' first."
        )
    if config.TAILOR_MODE != "minimal":
        return
    # Minimal mode renders from base_resume.yaml, so a base that does not match the
    # document is fatal — and it must be caught HERE, before the heavy calls. It used
    # to surface at render time, after the model work had been paid for, or not at all.
    if not os.path.exists(minimal_tailor.BASE_YAML_PATH):
        raise errors.SetupError(
            f"minimal mode needs {os.path.basename(minimal_tailor.BASE_YAML_PATH)}, which does "
            "not exist. `python run.py bootstrap --resume <your_resume.docx>` writes it from "
            "your document, or set `tailor_mode: rebuild` in user_config.yaml.")
    blocking = [detail for status, detail, _ in _check_base_resume(minimal_tailor.BASE_YAML_PATH)
                if status == _FAIL]
    if blocking:
        raise errors.SetupError(
            "base_resume.yaml does not match your résumé:\n  - " + "\n  - ".join(blocking))


def _send_confirmation_request(company, role, url, eligibility, jd_profile, pending_id,
                               prefit=None):
    """The approval message, sent **before** anything is generated: text and buttons only,
    no docx.

    The policy this replaced: we used to rewrite all the bullets first and then ask only about
    the postings fit_gate had marked "review". In practice that meant most postings were built
    without asking at all, and the user's complaint was exactly that — too many drafts. Now we
    **ask first**, and the expensive steps (company research + selection/rewrite + recruiter
    review + docx) only start once the button is pressed.

    The message also carries the **pre-fit judgement** (fit_gate.assess_prefit). It used to
    carry only eligibility, work authorisation and must-have requirements, on the theory that
    "gaps and fit only exist once the draft does". Measured: 13 of 87 requests got a reply
    (15%), and all 8 approvals were Product/Strategy roles — the user was deciding on the
    shape of the role, and the message was not telling them that. Fit can be judged before
    generation by the light backend (free when it is a local model).

    ⚠️ This function is only used when `config.DIGEST_HOURS` is empty (send-immediately mode).
    The default path queues requests and digest.py sends them batched twice a day — we changed
    that after one day produced 15 separate messages.

    Why visa_caution goes out here too: this path never calls report.send() (and never will if
    the user presses "skip" or simply does not answer), so a warning such as an export-control
    restriction could otherwise never reach them. Measured: one posting really did carry an
    ITAR U.S.-person requirement."""
    import notify
    verdict = (eligibility or {}).get("degree_eligible", "unclear")
    ask = fit_gate.should_ask_record(
        {"prefit": prefit, "eligibility": eligibility})
    header = ("<b>🤔 Build a resume draft for this?</b>" if ask
              else "<b>🔕 Not asking about this posting</b>")
    lines = [f"{header}\n{notify.esc(company)} · {notify.esc(role)}"]

    if prefit:
        tier = prefit.get("tier", "mid")
        glyph = {"high": "🟢", "mid": "🟡", "low": "⚪"}.get(tier, "🟡")
        line = f"{glyph} {fit_gate.TIER_LABEL.get(tier, 'possible fit')}"
        if prefit.get("reason"):
            line += f"\n{notify.esc(notify.clip(prefit['reason'], 180))}"
        lines.append(line)
        if prefit.get("unmet"):
            lines.append("<b>Requirements not met</b>\n" +
                         "\n".join(f"• {notify.esc(notify.clip(u, 90))}" for u in prefit["unmet"]))

    verdict_label = {"yes": "✅ Degree requirement met", "no": "❌ Degree requirement looks unmet",
                     "unclear": "❔ Degree requirement unclear"}.get(
                         verdict, "❔ Degree requirement unclear")
    if (prefit or {}).get("eligibility_override"):
        # The eligibility call came back 'no', but the posting's title or requirements name
        # the applicant's degree explicitly, so we ask anyway.
        verdict_label = ("❗ Judged ineligible, but the posting names the degree "
                         "(the judgement may be wrong)")
    reason = (eligibility or {}).get("reasoning") or ""
    lines.append(verdict_label + (f"\n{notify.esc(notify.clip(reason, 180))}" if reason else ""))

    if eligibility and eligibility.get("sponsorship_excluded"):
        quote = (eligibility.get("sponsorship_quote") or "").strip()
        lines.append("⚠️ <b>Posting states applicants needing sponsorship are not eligible</b>"
                     "\n" + notify.esc(notify.clip(quote, 200)))
    if eligibility and eligibility.get("visa_caution"):
        visa_note = (eligibility.get("visa_note") or "").strip()
        lines.append("⚠️ <b>Work authorisation caution</b>\n"
                     + notify.esc(notify.clip(visa_note, 200)))

    must_have = [m for m in (jd_profile or {}).get("must_have_skills", []) if m][:4]
    if must_have:
        lines.append("<b>Must-have requirements</b>\n" +
                     "\n".join(f"• {notify.esc(notify.clip(m, 90))}" for m in must_have))

    if url:
        lines.append(f'<a href="{notify.esc(url)}">Open the posting</a>')

    # When the applicant is ineligible or the fit is weak, do not make them press "skip" —
    # ignoring the message is already the answer. Asking ✅/⏭️ about every posting got 5
    # replies out of 24 (21%) and left 20 unanswered. One measured case: the gate judged 'no'
    # and even quoted the requirement it was failing (an undergraduate major the applicant
    # does not have), and we still asked — a decision nobody had any reason to make sat in
    # the queue. One button stays, so a human can overturn a wrong judgement.
    if not ask:
        lines.append("<i>Not asking: eligibility or fit looks off. Press the button below if "
                     "you want a draft anyway — otherwise this is skipped.</i>")
        notify.send_message("\n\n".join(lines),
                            keyboard=[[("Build a draft anyway", f"jw:make:{pending_id}")]])
        return

    lines.append("<i>✅ starts company research, the bullet rewrite and the recruiter review "
                 "(3-6 min).</i>")
    notify.send_message("\n\n".join(lines), keyboard=[[
        ("✅ Build a draft", f"jw:make:{pending_id}"),
        ("⏭️ Skip", f"jw:skip:{pending_id}"),
    ]])


def _template_chars_budget(doc, db_sections):
    """Measure the total bullet character count of the sections we are about to rewrite, in
    the template itself, and use that as the generation budget (see
    tailor_engine._format_total_budget). If the template is not shaped the way we expect,
    return None quietly: with no budget the prompt simply drops that one rule and the rest of
    the pipeline runs unchanged."""
    try:
        total = 0
        headers = {s["header_text"] for s in db_sections}
        for section in db_sections:
            _, bullets = resume_writer.find_section_block(
                doc, section["header_text"], stop_texts=headers)
            total += sum(len(p.text.strip()) for p in bullets)
        return total or None
    except (ValueError, KeyError) as e:
        print(f"  [warning] could not measure the template's character budget (skipped): {e}")
        return None


def cmd_tailor(args):
    auto = getattr(args, "auto", False)
    # Check the setup FIRST. Every one of these problems used to surface after the
    # posting had been fetched and classified — two model calls spent on a run that
    # could never finish.
    _validate_inputs(args.resume or _resume_template_path(config.DEFAULT_RESUME_PATH))
    # Two backends, two different jobs (see llm_client.py). Classification follows
    # --backend; writing the sentences you submit uses the heavy role, which
    # prefers a frontier model and says so when it has to fall back.
    backend = llm_client.get_backend(getattr(args, "backend", None), role="light", log=print)
    heavy_backend = llm_client.get_heavy_backend(getattr(args, "heavy_backend", None), log=print)

    # In auto mode every Telegram output is collected into this one Report and sent **once,
    # at the end** (see the comment at the top of notify.py). Firing a message per step
    # produced up to 6 messages for a single posting, which lost all context on a phone.
    report = None
    deferred = False  # True once we hand off to the approval gate — finally skips report.send()
    if auto:
        import notify
        notify.silent(f"[auto-tailor] starting: backend={backend.name}, "
                      f"heavy_backend={heavy_backend.name}")
        report = notify.Report(args.company, args.role, url=getattr(args, "job_url", None))

    try:
        jd_text = _get_jd_text(args)

        print("\nChecking whether the applicant's degree qualifies...")
        eligibility = jd_extractor.assess_eligibility(backend, jd_text)
        verdict = eligibility.get("degree_eligible", "unclear")
        print(f"  verdict: {verdict}")
        if eligibility.get("quoted_requirement"):
            print(f"  quoted from the posting: \"{eligibility['quoted_requirement']}\"")
        if eligibility.get("reasoning"):
            print(f"  reasoning: {eligibility['reasoning']}")

        if report:
            report.set_eligibility(eligibility)

        if verdict == "no" and not auto:
            # Default no: the posting says it does not admit this degree, so a run
            # with nobody watching should not spend the heavy calls on it.
            if not ask.confirm(
                "\n  This posting looks like a mismatch for the applicant's degree. "
                "Tailor the resume anyway? (y/N): ", default=False):
                print("  Stopped.")
                return
        # auto mode does not stop here (fail-open — a human makes the final call). The
        # warnings travel together on the report's eligibility / work-authorisation lines.

        if eligibility.get("visa_caution"):
            print(f"  [work authorisation] {(eligibility.get('visa_note') or '').strip()}")
        if eligibility.get("sponsorship_excluded"):
            print("  [sponsorship] this posting states applicants who need sponsorship are "
                  "not eligible (an internship under the applicant's own authorisation may "
                  "still be possible, but the post-graduation path is closed; see "
                  "profile.yaml)")
            print(f"    quoted: \"{eligibility.get('sponsorship_quote', '')}\"")

        print("\nAnalysing the posting (what candidate it is describing)...")
        jd_profile = jd_extractor.extract_jd_profile(backend, jd_text)

        company = args.company or jd_profile.get("company") or "Unknown"
        role = args.role or jd_profile.get("role") or "Unknown"
        if report:
            # Called without --company/--role (a manual --url run, say): use the names the
            # LLM pulled out of the posting instead.
            report.company, report.role = company, role
        print(f"  company: {company} / role: {role}")
        print(f"  key terms (LLM's reading): {', '.join(jd_profile.get('keywords', [])[:8])}")

        keyword_counts = keyword_extractor.extract_keywords(jd_text)
        print("  repeated terms (by frequency, for ATS matching): " +
              ", ".join(f"{t}×{c}" for t, c in keyword_counts[:10]))

        resume_path = args.resume or _resume_template_path(config.DEFAULT_RESUME_PATH)
        _validate_inputs(resume_path)
        doc = docx.Document(resume_path)
        db_sections = resume_db.load_db(config.DB_PATH)
        chars_budget = _build_sections_budget(doc, db_sections)
        if chars_budget:
            print(f"  length budget (measured from the template): rewritten bullets must total "
                  f"{chars_budget} characters or fewer")

        if auto:
            # ── Approval gate ────────────────────────────────────────────────────
            # Everything up to here is the light backend (free when it is a local model) plus
            # file reads. The expensive steps — company research, the bullet rewrite on the
            # heavy backend, 2-5 minutes per posting measured — and the docx only start after
            # the user presses the button, in confirm_handler → generate_from_pending().
            # This replaced fit_gate-based selective asking: the user's instruction was that
            # too many drafts were being produced and nothing substantial should run before
            # their approval.
            print(f"\n  judging fit first ({backend.name})...")
            prefit = fit_gate.assess_prefit(backend, jd_profile, db_sections, eligibility)

            pending_id = confirm_state.register({
                "company": company, "role": role,
                "url": getattr(args, "job_url", None),
                "jd_profile": jd_profile, "keyword_counts": keyword_counts,
                "chars_budget": chars_budget, "resume_path": resume_path,
                "eligibility": eligibility, "jd_text": jd_text, "prefit": prefit,
            })
            # If this posting is already pending, register returns the existing id — refresh
            # it with the latest judgement anyway, so records queued before a change to the
            # fit logic do not reach the digest with stale verdicts.
            confirm_state.update(pending_id, prefit=prefit)

            if config.DIGEST_HOURS:
                # The default path: queue it and let digest.py send one batched message twice
                # a day. Sending immediately produced 15 notifications in a single day (see
                # the comments in digest.py).
                print(f"\nQueued for the digest ({pending_id}) — "
                      f"will be sent batched at {config.DIGEST_HOURS}.")
            else:
                _send_confirmation_request(company, role, getattr(args, "job_url", None),
                                           eligibility, jd_profile, pending_id, prefit=prefit)
                print("\nWaiting for approval — pressing ✅ in Telegram starts generation.")
            deferred = True
            return

        print("\nResearching the company (news-based red-flag scan, heavy backend)...")
        research = company_research.research_company(heavy_backend, company)
        if research["notable"]:
            print(f"  [note] {research['notes']}")
        else:
            print("  nothing notable")
        jd_profile["company_research"] = research
        if report:
            report.set_research(research)

        result, _base = _build_sections(heavy_backend, jd_profile, db_sections,
                                        keyword_counts, chars_budget,
                                        keep_ids=getattr(args, "keep", None) or [])
        gaps = result["gaps"]

        if gaps and not auto:
            answers = tailor_engine.run_gap_qa(gaps)
            if answers:
                print("\nRewriting with your answers folded in...")
                result, _base = _build_sections(heavy_backend, jd_profile, db_sections,
                                                keyword_counts, chars_budget,
                                                extra_facts=answers)
                gaps = result["gaps"]
        # auto mode never asks about gaps; it leaves them in place so the report's
        # "requirements not met" block shows them.
        if report:
            report.set_gaps(gaps)

        # auto mode already returned at the approval gate above. Everything below is the path
        # where a human ran the CLI themselves, which is approval in itself.
        if config.RECRUITER_REVIEW and not getattr(args, "no_recruiter", False):
            result["sections"], review_data = recruiter_review.run(
                llm_client.get_polish_backend(), company, role, jd_profile,
                result["sections"], db_sections, jd_text=jd_text,
                keyword_counts=keyword_counts, total_chars_budget=chars_budget,
                polish_enabled=config.RECRUITER_POLISH,
            )
            result["recruiter_review"] = review_data
            if report:
                report.set_recruiter(review_data)

        if not tailor_engine.review_and_confirm(result, db_sections, auto=auto):
            print("\nCancelled. Adjust resume_db.yaml or the JD and run it again.")
            return

        _finalize_and_send(doc, result, db_sections, company, role, resume_path, jd_text, jd_profile,
                           report, shorten_backend=heavy_backend)

    except Exception as e:
        if not report:
            raise
        # Even on failure, what we gathered so far (eligibility, work authorisation, company
        # news, gaps) is already on the report and goes out in the finally below. This is
        # deliberately not an all-or-nothing send.
        report.set_error(str(e))
        print(f"\nFailed: {e}")
    finally:
        if report and not deferred:
            # A human always reviews the draft on their phone. No code path here ever calls
            # an application-submission API.
            report.send()


def generate_from_pending(record, short_id=None):
    """Build the draft for real, after the ✅ button was pressed — the expensive steps run
    **for the first time here**.

    Everything stored in confirm_state is output from the cheap local steps (jd_profile,
    keyword_counts, eligibility, jd_text, chars_budget, prefit). None of the company research
    (1 heavy call), the selection/rewrite (2 heavy calls), the recruiter review and polish
    (2 more, on the strongest model available) or the docx rendering has happened yet. So we
    do all of that here and send a single report, sharing `_finalize_and_send` with
    `cmd_tailor`'s manual path.

    On failure, what we gathered so far stays on the report and goes out with it — the same
    principle as cmd_tailor's exception handling. The report also carries a **retry button**,
    and if the run got as far as a successful rewrite we store that result on the record so
    **a retry costs no further LLM calls**. Before that, a failure re-ran both ~52KB prompts
    from scratch.

    Returns whether it succeeded. The caller (confirm_handler) uses that to decide whether to
    delete the pending record. It used to delete unconditionally, which meant a failed
    generation left no way to retry: in one case the heavy CLI died with exit 1 an hour and a
    half after approval, and the record went with it.
    """
    import notify

    company, role = record["company"], record["role"]
    report = notify.Report(company, role, url=record.get("url"))
    report.set_eligibility(record.get("eligibility") or {})
    heavy_backend = llm_client.get_heavy_backend()
    jd_profile = record.get("jd_profile") or {}
    saved_result = record.get("result")

    try:
        resume_path = _resume_template_path(record.get("resume_path") or config.DEFAULT_RESUME_PATH)
        _validate_inputs(resume_path)

        notify.silent(f"[confirm] approved — starting generation: {company} · {role}")

        doc = docx.Document(resume_path)
        db_sections = resume_db.load_db(config.DB_PATH)

        if saved_result:
            # The retry path: the rewrite already finished, so we resume at rendering with
            # zero LLM calls.
            print("\nReusing the saved rewrite (no LLM calls; resuming at rendering)")
            result = saved_result
            research = jd_profile.get("company_research")
            if research:
                report.set_research(research)
            if record.get("recruiter_review"):
                report.set_recruiter(record["recruiter_review"])
        else:
            print("\nResearching the company (news-based red-flag scan, heavy backend)...")
            research = company_research.research_company(heavy_backend, company)
            if research["notable"]:
                print(f"  [note] {research['notes']}")
            jd_profile["company_research"] = research
            report.set_research(research)

            result, _base = _build_sections(
                heavy_backend, jd_profile, db_sections,
                record.get("keyword_counts"), record.get("chars_budget"),
                keep_ids=record.get("keep_ids") or [])
            # This was the most expensive stretch — save it immediately so a later failure
            # does not make us pay for it twice.
            if short_id:
                confirm_state.update(short_id, result=result, jd_profile=jd_profile)

            if config.RECRUITER_REVIEW:
                result["sections"], review_data = recruiter_review.run(
                    llm_client.get_polish_backend(), company, role, jd_profile,
                    result["sections"], db_sections,
                    jd_text=record.get("jd_text", ""),
                    keyword_counts=record.get("keyword_counts"),
                    total_chars_budget=record.get("chars_budget"),
                    polish_enabled=config.RECRUITER_POLISH,
                )
                report.set_recruiter(review_data)
                result["recruiter_review"] = review_data
                if short_id:
                    confirm_state.update(short_id, result=result, recruiter_review=review_data)

        report.set_gaps(result.get("gaps"))
        _finalize_and_send(doc, result, db_sections, company, role, resume_path,
                           record.get("jd_text", ""), jd_profile, report,
                           shorten_backend=heavy_backend)
        return True
    except Exception as e:
        report.set_error(str(e))
        if short_id:
            report.set_keyboard([[("🔁 Retry", f"jw:make:{short_id}")]])
        print(f"\nFailed: {e}")
        return False
    finally:
        report.send()


def cmd_review(args):
    """**Review an existing draft the way a recruiter would, and optionally polish it** —
    manual only.

    The automatic path (generate_from_pending) already does this during generation. This
    command is for re-reviewing drafts produced before the step existed, or a docx a human
    has edited by hand.

    How bullet ids are recovered: (1) use `<company>_<role>_result.json` if tailor left one;
    (2) otherwise match the docx paragraphs against the DB by word overlap
    (recruiter_review.match_docx_bullets). With `--apply`, the sentences are rewritten
    according to the review, the docx is regenerated and the page count is measured again."""
    import notify

    company, role = args.company, args.role
    safe_company, safe_role = _safe_name(company), _safe_name(role)
    db_sections = resume_db.load_db(config.DB_PATH)

    if args.docx:
        if not os.path.exists(args.docx):
            print(f"docx not found: {args.docx}")
            sys.exit(1)
        docx_path = args.docx
    else:
        # Filenames carry the date, so match on the prefix and take the newest.
        out_dir = _company_output_dir(company)
        prefix = f"Resume_{safe_company}_{safe_role}_"
        matches = sorted(
            (os.path.join(out_dir, n) for n in os.listdir(out_dir)
             if n.startswith(prefix) and n.endswith(".docx")),
            key=os.path.getmtime, reverse=True)
        if not matches:
            print(f"no draft found: {out_dir}/{prefix}*.docx")
            sys.exit(1)
        docx_path = matches[0]
    print(f"reviewing: {docx_path}")

    profile_path = _find_output_file(company, f"{safe_company}_{safe_role}_jd_profile.json")
    if not os.path.exists(profile_path):
        print(f"JD profile not found: {profile_path} (run tailor first)")
        sys.exit(1)
    with open(profile_path, encoding="utf-8") as f:
        jd_profile = json.load(f)

    jd_text = ""
    jd_text_path = _find_output_file(company, f"{safe_company}_{safe_role}_jd_text.txt")
    if os.path.exists(jd_text_path):
        with open(jd_text_path, encoding="utf-8") as f:
            jd_text = f.read()
    keyword_counts = keyword_extractor.extract_keywords(jd_text) if jd_text else []

    result_path = _find_output_file(company, f"{safe_company}_{safe_role}_result.json")
    doc = docx.Document(docx_path)
    if os.path.exists(result_path):
        with open(result_path, encoding="utf-8") as f:
            sections = json.load(f).get("sections") or {}
        print(f"  bullet source: {os.path.basename(result_path)}")
    else:
        sections = recruiter_review.match_docx_bullets(doc, db_sections)
        print("  bullet source: reverse-matched from the docx (no result.json)")
    if not sections:
        print("found no bullets to review.")
        sys.exit(1)

    backend = llm_client.get_polish_backend()
    chars_budget = _template_chars_budget(docx.Document(
        args.resume or config.DEFAULT_RESUME_PATH), db_sections)

    with genlock.GenerationLock(label=f"review {company} · {role}"):
        if args.apply:
            new_sections, review_data = recruiter_review.run(
                backend, company, role, jd_profile, sections, db_sections,
                jd_text=jd_text, keyword_counts=keyword_counts,
                total_chars_budget=chars_budget)
        else:
            review_data = recruiter_review.review(
                backend, company, role, jd_profile, sections, db_sections,
                jd_text=jd_text, keyword_counts=keyword_counts)
            new_sections = sections

        out_dir = _company_output_dir(company)
        audit = recruiter_review.save_audit(out_dir, company, role, review_data, _safe_name)
        print(f"\nfull review saved: {audit}")
        print(f"\n[6-second scan] {(review_data or {}).get('first_impression', '')}")
        print(f"[competitive context] {(review_data or {}).get('competitive_context', '')}")
        for m in (review_data or {}).get("missing_signal") or []:
            print(f"  [signal not visible] {m}")
        for a in (review_data or {}).get("ats_misses") or []:
            print(f"  [ATS miss] {a}")

        if not args.apply:
            print("\n(review only — pass --apply to actually rewrite the sentences)")
            return

        # Apply the polish to the docx and measure the page count again.
        template_path = args.resume or config.DEFAULT_RESUME_PATH
        _validate_inputs(template_path)
        rendered = docx.Document(template_path)
        result = {"sections": new_sections, "gaps": [], "cuts": {}, "close_calls": [],
                  "recruiter_review": review_data}
        report = notify.Report(company, role) if args.notify else None
        if report:
            report.set_recruiter(review_data)
        out_path = _finalize_and_send(
            rendered, result, db_sections, company, role, template_path,
            jd_text, jd_profile, report, shorten_backend=llm_client.get_heavy_backend())
        if report:
            report.send()
        print(f"\npolish applied: {out_path}")


def cmd_digest(args):
    """Send the queued approval requests now, batched (manual trigger). Normally bot_listener
    calls this automatically at the `config.DIGEST_HOURS` times."""
    import digest

    sent = digest.send_if_due(force=True)
    if not sent:
        print("no pending requests to send.")


def cmd_decisions(args):
    """Summarise the decision journal: what the user chose to build and what they passed on.
    This is the signal an upstream filter can learn from."""
    summary = decisions.summary()
    counts = summary["counts"]
    print(f"{summary['total']} total — ✅ {counts['make']} / ⏭️ {counts['skip']} / "
          f"expired {counts['expire']}")
    for r in summary["rows"][:args.limit]:
        glyph = {"make": "✅", "skip": "⏭️", "expire": "⌛"}.get(r["decision"], "?")
        prefit = f" fit={r.get('prefit')}" if r.get("prefit") else ""
        print(f"  {r['ts'][:16]} {glyph} {r['company']} · {r['role']}{prefit}")


def cmd_polish(args):
    """Rewrite an existing draft again on the heavy backend — fully manual, with no automatic
    path to it. Run it only when you have decided to spend the quota deliberately."""
    profile_path = args.profile_json or _find_output_file(
        args.company, f"{_safe_name(args.company)}_{_safe_name(args.role)}_jd_profile.json"
    )
    if not os.path.exists(profile_path):
        print(f"JD profile not found: {profile_path} (run tailor first)")
        sys.exit(1)
    with open(profile_path, "r", encoding="utf-8") as f:
        jd_profile = json.load(f)

    # If tailor saved the raw JD text, recompute the repeated keywords from it. Output from
    # older runs that lacks the file is skipped quietly — this is nice to have, not required.
    jd_text_path = _find_output_file(
        args.company, f"{_safe_name(args.company)}_{_safe_name(args.role)}_jd_text.txt"
    )
    keyword_counts = []
    if os.path.exists(jd_text_path):
        with open(jd_text_path, "r", encoding="utf-8") as f:
            keyword_counts = keyword_extractor.extract_keywords(f.read())
        print("  repeated terms (by frequency, for ATS matching): " +
              ", ".join(f"{t}×{c}" for t, c in keyword_counts[:10]))

    resume_path = args.resume or config.DEFAULT_RESUME_PATH
    if not resume_path:
        print("no resume template configured — run "
              "'python run.py bootstrap --resume <your_resume.docx>' first.")
        sys.exit(1)
    doc = docx.Document(resume_path)
    db_sections = resume_db.load_db(config.DB_PATH)
    chars_budget = _template_chars_budget(doc, db_sections)

    backend = llm_client.get_heavy_backend(getattr(args, "heavy_backend", None), log=print)
    print(f"\nrewriting with {backend.name}...")
    result = tailor_engine.generate_section_revisions(
        backend, jd_profile, db_sections, keyword_counts=keyword_counts,
        max_per_section=config.MAX_BULLETS_PER_SECTION, total_chars_budget=chars_budget,
    )
    gaps = result["gaps"]
    if gaps:
        answers = tailor_engine.run_gap_qa(gaps)
        if answers:
            result = tailor_engine.generate_section_revisions(
                backend, jd_profile, db_sections, extra_facts=answers,
                keyword_counts=keyword_counts, max_per_section=config.MAX_BULLETS_PER_SECTION,
                total_chars_budget=chars_budget,
            )

    if not tailor_engine.review_and_confirm(result, db_sections, auto=False):
        print("\nCancelled.")
        return

    result["sections"] = _clamp_to_caps(result["sections"], config.MAX_BULLETS_PER_SECTION)
    applied = resume_writer.render_sections(doc, result, db_sections)
    date_str = datetime.date.today().strftime("%Y%m%d")
    out_path = os.path.join(
        _company_output_dir(args.company),
        f"Resume_{_safe_name(args.company)}_{_safe_name(args.role)}_{date_str}_polished.docx",
    )
    resume_writer.save(doc, out_path)
    _copy_to_sync_dir(args.company, out_path)
    print("\nRunning format checks (em dashes / fonts / character counts / page headroom)...")
    validate.validate_docx(out_path, baseline_path=resume_path)
    total_bullets = sum(len(ids) for ids in applied.values())
    print(f"\nDone: {out_path}")
    print(f"  bullets selected: {total_bullets} in total")
    for section_name, ids in applied.items():
        print(f"    {section_name}: {len(ids)} {ids}")


def _slug(name):
    s = "".join(ch for ch in name.lower() if ch.isalnum())
    return s[:10] or "sec"


DB_HEADER_COMMENT = """\
# Raw experience database — the source of truth.
#
# Generated from your resume by 'python run.py bootstrap'. Each bullet is stored as a whole
# finished sentence, not split into atomic facts: keeping the sentence intact is what keeps
# the tool from inventing facts. Bullets are tagged on three axes: tags.org, keywords and
# content_type. content_type is free vocabulary, not a fixed enum — invent a new value when
# you add an experience that needs one.
#
# header_text is the literal key used to locate this section's block inside the template
# docx (config.DEFAULT_RESUME_PATH, i.e. resume_path in user_config.yaml). Change the
# template and you must change header_text to match.
#
# Editing this file by hand — fixing wording, adding a new bullet — is the normal workflow.

"""


def cmd_bootstrap(args):
    resume_path = os.path.abspath(args.resume)
    if not os.path.exists(resume_path):
        print(f"resume file not found: {resume_path}")
        sys.exit(1)

    backend = llm_client.get_backend(getattr(args, "backend", None), role="light", log=print)
    print(f"backend: {backend.name}")

    doc = docx.Document(resume_path)
    try:
        blocks = resume_parser.parse_experience_blocks(doc, start_after=args.section_heading)
    except ValueError as e:
        print(f"parsing failed: {e}")
        sys.exit(1)

    if not blocks:
        print("found no experience blocks — check your resume's formatting "
              "(a company/location line must be followed by bulleted paragraphs).")
        sys.exit(1)

    print(f"\nDetected {len(blocks)} section(s):")
    for b in blocks:
        name = resume_parser.section_name_from_header(b["header_text"])
        print(f"  - {name} ({b['header_text']!r}): {len(b['bullets'])} bullets")
    if not ask.confirm("\nProceed with this structure? (Y/n): ", default=True):
        print("Stopped. Check --section-heading or your resume's formatting and try again.")
        sys.exit(0)

    sections = []
    for b in blocks:
        name = resume_parser.section_name_from_header(b["header_text"])
        prefix = _slug(name)
        bullets = [
            {"id": f"{prefix}-b{i}", "text": text}
            for i, text in enumerate(b["bullets"], 1)
        ]
        sections.append({"name": name, "header_text": b["header_text"], "bullets": bullets})

    print("\nTagging the bullets and looking for gaps (LLM)...")
    tags_by_id = bootstrap_engine.tag_and_find_gaps(backend, sections)

    gaps = []
    for section in sections:
        for bullet in section["bullets"]:
            info = tags_by_id.get(bullet["id"], {})
            bullet["tags"] = {
                "org": section["name"],
                "keywords": info.get("keywords", []),
                "content_type": info.get("content_type", []),
            }
            gap = info.get("gap")
            if gap and gap.get("question"):
                gaps.append({"bullet": bullet, "question": gap["question"]})

    if gaps:
        print(f"\n[{len(gaps)} gap(s)] Answer and it goes into the bullet "
              "(press Enter to skip one):")
        for g in gaps:
            bullet = g["bullet"]
            print(f"\n- {bullet['text'][:100]}")
            print(f"  question: {g['question']}")
            answer = ask.line("  answer: ")
            if answer:
                bullet["text"] = bootstrap_engine.refine_bullet_with_fact(
                    backend, bullet["text"], g["question"], answer,
                )
                print(f"  -> applied: {bullet['text'][:100]}")

    if os.path.exists(config.DB_PATH):
        backup_path = config.DB_PATH + ".bak"
        os.replace(config.DB_PATH, backup_path)
        print(f"\nBacked up the existing {config.DB_PATH} to {backup_path}.")

    yaml_body = yaml.safe_dump(
        {"sections": sections}, allow_unicode=True, sort_keys=False,
        default_flow_style=False, width=100000,
    )
    with open(config.DB_PATH, "w", encoding="utf-8") as f:
        f.write(DB_HEADER_COMMENT + yaml_body)

    _write_user_config(resume_path, sections)
    wrote_base = _write_base_resume_draft(resume_path, sections)

    total_bullets = sum(len(s["bullets"]) for s in sections)
    print(f"\nDone: wrote {config.DB_PATH} ({total_bullets} bullets) and "
          f"{config.USER_CONFIG_PATH}.")
    print("\nNext:")
    print(f"  1. open {os.path.basename(config.DB_PATH)} and fill in each bullet's `context` — "
          "it is the background a rewrite draws on AND the list of numbers the tool is "
          "allowed to use. See resume_db.example.yaml.")
    if wrote_base:
        print(f"  2. check {os.path.basename(minimal_tailor.BASE_YAML_PATH)} — it was written "
              "from your document, so it lists every bullet your resume shows. Remove any "
              "line you do not want treated as approved.")
    else:
        print(f"  2. {os.path.basename(minimal_tailor.BASE_YAML_PATH)} already existed and was "
              "left alone.")
    print("  3. .venv/bin/python run.py doctor")
    print("  4. .venv/bin/python run.py tailor --jd-file posting.txt --company \"Acme\" "
          "--role \"Product Manager Intern\"")


def _write_user_config(resume_path, sections):
    """Update user_config.yaml **without throwing away what is already in it**.

    Bootstrap used to write the file from scratch, which silently deleted every
    other setting — `base_resume_path`, `tailor_mode`, the backend choice. A user
    following the setup guide in order (write user_config, then bootstrap) lost
    all of it and then found minimal mode refusing to run. Now only the two keys
    bootstrap actually derives are touched, and the previous file is kept as a
    .bak so nothing is unrecoverable.
    """
    derived = {
        "resume_path": resume_path,
        "max_bullets_per_section": {s["name"]: len(s["bullets"]) for s in sections},
    }
    existing = {}
    if os.path.exists(config.USER_CONFIG_PATH):
        with open(config.USER_CONFIG_PATH, "r", encoding="utf-8") as f:
            existing = yaml.safe_load(f) or {}
        backup = config.USER_CONFIG_PATH + ".bak"
        shutil.copy2(config.USER_CONFIG_PATH, backup)
        kept = [k for k in existing if k not in derived]
        print(f"\nUpdated {os.path.basename(config.USER_CONFIG_PATH)}: set resume_path and "
              f"max_bullets_per_section, kept your other {len(kept)} setting(s)"
              + (f" ({', '.join(sorted(kept))})" if kept else "")
              + f". Previous file saved as {os.path.basename(backup)}.")
    else:
        # First run: point minimal mode at the same document, so the default mode
        # works without a second edit. Both keys are yours to change afterwards.
        derived["base_resume_path"] = resume_path
        derived["tailor_mode"] = "minimal"

    merged = {**existing, **derived}
    with open(config.USER_CONFIG_PATH, "w", encoding="utf-8") as f:
        f.write("# Your settings. Gitignored: never committed, never shared.\n")
        f.write("# `python run.py bootstrap` sets resume_path and max_bullets_per_section;\n")
        f.write("# every other key here is yours and is preserved across re-runs.\n")
        f.write("# See user_config.example.yaml for every key and what it does.\n")
        yaml.safe_dump(merged, f, allow_unicode=True, sort_keys=False, default_flow_style=False)


BASE_HEADER_COMMENT = """\
# base_resume.yaml — the résumé you have already approved, as data.
#
# Written by `run.py bootstrap` from your own document, so it starts out as an
# exact copy of what your résumé shows today. Minimal mode treats every sentence
# here as the quality floor: an edit that fails validation is discarded and the
# sentence below is what ships.
#
# Review it once:
#   - remove any line you do not want treated as approved
#   - keep each `text` identical to resume_db.yaml and to the document itself
#     (the same sentence in three places is how drift gets detected)
#   - keep each `header_text` identical to the heading in the .docx, character
#     for character, or the renderer cannot find the section
#
# Gitignored: this is your career data and is never committed.

"""


def _write_base_resume_draft(resume_path, sections):
    """Write base_resume.yaml from the parsed document. Returns True if written.

    Minimal mode is the default and cannot run without this file, and the setup
    guide used to say "write it by hand" — every `header_text` matching a .docx
    paragraph character for character, every id matching the database. Bootstrap
    has just parsed that exact document, so it can produce a correct file for
    free, and a hand-made one was the main source of "header not found" errors.

    An existing file is never overwritten: it holds decisions about which bullets
    are approved, which bootstrap cannot know.
    """
    path = minimal_tailor.BASE_YAML_PATH
    if os.path.exists(path):
        return False
    base = {
        "source_docx": resume_path,
        "sections": [
            {
                "name": s["name"],
                "header_text": s["header_text"],
                "bullets": [{"id": b["id"], "text": b["text"]} for b in s["bullets"]],
            }
            for s in sections
        ],
    }
    body = yaml.safe_dump(base, allow_unicode=True, sort_keys=False,
                          default_flow_style=False, width=100000)
    with open(path, "w", encoding="utf-8") as f:
        f.write(BASE_HEADER_COMMENT + body)
    return True


# ── doctor ──────────────────────────────────────────────────────────────────

_OK, _WARN, _FAIL = "ok  ", "warn", "FAIL"


def _line(status, label, detail=""):
    print(f"  [{status}] {label}" + (f" — {detail}" if detail else ""))
    return status


def _check_base_resume(base_yaml):
    """Validate base_resume.yaml against the database and the document.

    The two rules base_resume.example.yaml calls hard errors — every id exists in
    the database, every header_text matches a .docx heading character for
    character — used to be checked nowhere. They surfaced as a render failure
    after the expensive calls had run, or worse: a base listing fewer bullets
    than the document produced a shorter résumé that every check called PASS.

    Returns a list of (status, detail, fix) rows.
    """
    rows = []
    try:
        base = minimal_tailor.load_base(base_yaml)
    except Exception as e:
        return [(_FAIL, f"will not load: {e}", "compare against base_resume.example.yaml")]

    base_sections = base.get("sections", [])
    pinned = sum(len(sec.get("bullets", [])) for sec in base_sections)

    # rule 1: every id exists in the database
    try:
        db_sections = resume_db.load_db(config.DB_PATH)
    except Exception:
        db_sections = None
    if db_sections is not None:
        db_ids = {b["id"] for sec in db_sections for b in sec["bullets"]}
        unknown = [b["id"] for sec in base_sections for b in sec.get("bullets", [])
                   if b["id"] not in db_ids]
        if unknown:
            rows.append((_FAIL, f"{len(unknown)} id(s) are not in resume_db.yaml: "
                                + ", ".join(unknown[:4]),
                         "fix the ids, or add those bullets to resume_db.yaml"))
        # drift: the same sentence is meant to be in both files
        db_text = {b["id"]: b["text"].strip() for sec in db_sections for b in sec["bullets"]}
        drifted = [b["id"] for sec in base_sections for b in sec.get("bullets", [])
                   if b["id"] in db_text and b["text"].strip() != db_text[b["id"]]]
        if drifted:
            rows.append((_WARN, f"{len(drifted)} sentence(s) differ from resume_db.yaml: "
                                + ", ".join(drifted[:4]),
                         "make the text identical in both files, or the baseline is a lie"))

    # rule 2: every header_text is a heading in the document
    docx_path = config.BASE_RESUME_PATH or base.get("source_docx")
    if docx_path and os.path.exists(docx_path):
        try:
            doc = docx.Document(docx_path)
        except Exception as e:
            doc = None
            rows.append((_WARN, f"could not open {docx_path}: {e}", "check base_resume_path"))
        if doc is not None:
            paragraphs = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
            para_set = set(paragraphs)
            for sec in base_sections:
                header = (sec.get("header_text") or "").strip()
                if header in para_set:
                    continue
                near = [t for t in paragraphs if t.startswith(header[:12])] if header else []
                hint = f"; the document has {near[0]!r}" if near else ""
                rows.append((_FAIL,
                             f"header_text {header!r} is not a heading in the document{hint}",
                             "copy the heading out of the .docx and paste it in — the match "
                             "is character for character"))
            # content loss: a section listing fewer bullets than the document shows
            headers = {sec.get("header_text") for sec in base_sections}
            for sec in base_sections:
                header = (sec.get("header_text") or "").strip()
                if header not in para_set:
                    continue
                try:
                    _, doc_bullets = resume_writer.find_section_block(
                        doc, header, stop_texts=headers)
                except ValueError:
                    continue
                listed, actual = len(sec.get("bullets", [])), len(doc_bullets)
                if listed < actual:
                    rows.append((_WARN,
                                 f"section {sec.get('name')!r} lists {listed} of the "
                                 f"{actual} bullets in the document",
                                 "add the missing bullets, or the tailored résumé will be "
                                 "shorter than the one you approved"))

    # a section the base does not mention renders nothing, which minimal mode treats
    # as a broken file rather than an instruction to drop a job
    if db_sections is not None:
        covered = {(sec.get("name") or "").strip() for sec in base_sections}
        uncovered = [s["name"] for s in db_sections if s["name"] not in covered]
        if uncovered:
            rows.append((_FAIL,
                         "no entry for section(s) " + ", ".join(uncovered),
                         "add them (bootstrap writes the file from your document), or remove "
                         "them from resume_db.yaml if they are no longer on your résumé"))

    if not rows:
        rows.append((_OK, f"{pinned} bullets pinned, ids and headings all match", ""))
    return rows


def cmd_doctor(args):
    """Check everything this tool needs, and say exactly what to do about what
    is missing. Run it after setup, and again whenever something behaves oddly:
    every requirement here was something that silently produced a worse result
    rather than an error.
    """
    import importlib

    failures, warnings = [], []

    def record(status, label, detail="", fix=""):
        _line(status, label, detail)
        if status == _FAIL:
            failures.append(fix or label)
        elif status == _WARN:
            warnings.append(fix or label)

    print("\nPython and packages")
    v = sys.version_info
    record(_OK if v >= (3, 10) else _FAIL, f"python {v.major}.{v.minor}.{v.micro}",
           "" if v >= (3, 10) else "3.10 or newer required",
           "install a newer python and recreate the venv")
    for mod, why in (("docx", "reading and writing .docx"), ("yaml", "config files"),
                     ("requests", "fetching postings"), ("trafilatura", "posting text extraction"),
                     ("bs4", "posting text fallback")):
        try:
            importlib.import_module(mod)
            record(_OK, mod, why)
        except ImportError:
            record(_FAIL, mod, "not installed", "pip install -r requirements.txt")

    print("\nYour files")
    prof = profile_module.load(required=False)
    if not os.path.exists(prof.source):
        record(_FAIL, "profile.yaml", "missing",
               "cp profile.example.yaml profile.yaml, then fill it in")
    else:
        missing = prof.missing_fields()
        stale = prof.unedited_fields()
        if stale:
            # The quiet failure this catches: the example copied and never edited.
            # Every prompt would then judge postings against a fictional person.
            record(_WARN, "profile.yaml",
                   "still has profile.example.yaml's values for: " + ", ".join(stale),
                   "edit those fields in profile.yaml — they are describing someone who "
                   "does not exist")
        else:
            record(_WARN if missing else _OK, "profile.yaml",
                   ("incomplete: " + ", ".join(missing)) if missing else
                   f"{prof.program or 'degree'} / graduating {prof.graduation or '?'}",
                   "fill in the listed fields in profile.yaml")

    if not os.path.exists(config.USER_CONFIG_PATH):
        record(_FAIL, "user_config.yaml", "missing",
               "cp user_config.example.yaml user_config.yaml, or run "
               "`python run.py bootstrap --resume <your_resume.docx>`")
    else:
        record(_OK, "user_config.yaml", f"mode={config.TAILOR_MODE}")

    if not config.DEFAULT_RESUME_PATH:
        record(_FAIL, "resume_path", "not set in user_config.yaml",
               "set resume_path to your one-page .docx")
    elif not os.path.exists(config.DEFAULT_RESUME_PATH):
        record(_FAIL, "resume_path", f"file not found: {config.DEFAULT_RESUME_PATH}",
               "fix resume_path in user_config.yaml")
    else:
        record(_OK, "resume_path", config.DEFAULT_RESUME_PATH)

    if config.TAILOR_MODE == "minimal":
        if not config.BASE_RESUME_PATH:
            record(_FAIL, "base_resume_path", "required by minimal mode, not set",
                   "point base_resume_path at the .docx minimal mode should edit "
                   "(usually the same file as resume_path)")
        elif not os.path.exists(config.BASE_RESUME_PATH):
            record(_FAIL, "base_resume_path", f"file not found: {config.BASE_RESUME_PATH}",
                   "fix base_resume_path in user_config.yaml")
        else:
            record(_OK, "base_resume_path", config.BASE_RESUME_PATH)

    if not os.path.exists(config.DB_PATH):
        record(_FAIL, "resume_db.yaml", "missing",
               "python run.py bootstrap --resume <your_resume.docx>, then edit the result")
    else:
        try:
            sections = resume_db.load_db(config.DB_PATH)
            live = sum(1 for sec in sections for b in sec["bullets"] if not b.get("retired"))
            record(_OK, "resume_db.yaml",
                   f"{len(sections)} sections, {live} bullets available")
        except Exception as e:
            record(_FAIL, "resume_db.yaml", f"will not load: {e}",
                   "compare your file against resume_db.example.yaml")

    base_yaml = minimal_tailor.BASE_YAML_PATH
    if config.TAILOR_MODE == "minimal":
        if not os.path.exists(base_yaml):
            record(_FAIL, "base_resume.yaml", "missing — minimal mode cannot run",
                   "run `python run.py bootstrap --resume <your_resume.docx>`, which writes "
                   "it from your document")
        else:
            for status, detail, fix in _check_base_resume(base_yaml):
                record(status, "base_resume.yaml", detail, fix)

    print("\nLLM backends")
    availability = llm_client.available_backends()
    for name, ok in availability.items():
        # Informational: you only need ONE of these. A missing one is not a problem.
        _line(_OK if ok else "--  ", name, "available" if ok else "not installed/configured")
    for role in ("light", "heavy"):
        try:
            chosen = llm_client.resolve_name("auto", role=role)
            detail = chosen
            if role == "heavy" and chosen == "ollama":
                detail += " — a local model will be writing your resume sentences; a CLI or "\
                          "the API is strongly preferred here"
                record(_WARN, f"auto ({role})", detail,
                       "install the Claude Code CLI, configure codex-cli, or set ANTHROPIC_API_KEY")
            else:
                record(_OK, f"auto ({role})", detail)
        except llm_client.BackendUnavailable as e:
            record(_FAIL, f"auto ({role})", "nothing available", str(e).splitlines()[0])

    print("\nOne-page measurement")
    if pagefit.word_available():
        record(_OK, "Microsoft Word", "page count is measured, not estimated")
    else:
        record(_WARN, "Microsoft Word", "not found",
               "without Word the page count is an estimate and can be wrong at the "
               "boundary; see SETUP.md")

    print("\nNotifications (optional)")
    from vendor import telegram
    # Informational, never a warning: nothing here needs Telegram. It only adds
    # approve/skip buttons on your phone.
    _line(_OK if telegram.is_configured() else "--  ", "telegram",
          "configured" if telegram.is_configured() else
          "TG_BOT_TOKEN / TG_CHAT_ID unset — console only, approval buttons unavailable")

    if getattr(args, "probe", False):
        print("\nLive probe (one real call per role)")
        for role in ("light", "heavy"):
            try:
                backend = llm_client.get_backend(getattr(args, "backend", None), role=role)
                answer = backend.complete(
                    'Reply with this JSON and nothing else: {"ok": true}')
                parsed = llm_client.parse_json_loose(answer)
                record(_OK if parsed.get("ok") else _WARN, f"{role} ({backend.name})",
                       f"replied {answer[:60]!r}")
            except Exception as e:
                record(_FAIL, f"{role}", f"{type(e).__name__}: {e}",
                       "fix the backend, or pick another with --backend")

    print()
    if failures:
        print(f"{len(failures)} blocking problem(s):")
        for f in failures:
            print(f"  - {f}")
    if warnings:
        print(f"{len(warnings)} thing(s) that will degrade output:")
        for w in warnings:
            print(f"  - {w}")
    if not failures and not warnings:
        print("All good. Try: python run.py tailor --jd-file posting.txt --company Acme "
              "--role 'Product Manager Intern'")
    sys.exit(1 if failures else 0)


def main():
    parser = argparse.ArgumentParser(description="Resume Tailor CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p_tailor = sub.add_parser("tailor", help="tailor the resume to a job posting")
    p_tailor.add_argument("--url", help="Job Description URL")
    p_tailor.add_argument("--jd-file", help="path to a JD text file (instead of --url)")
    p_tailor.add_argument("--resume",
                          help="path to the source resume .docx "
                               "(default: config.DEFAULT_RESUME_PATH)")
    p_tailor.add_argument("--company", help="company name (extracted from the JD if omitted)")
    p_tailor.add_argument("--role", help="role title (extracted from the JD if omitted)")
    p_tailor.add_argument(
        "--job-url",
        help="URL of the posting — used only for the 'Open the posting' link in the auto-mode "
             "report; the JD itself is read from --jd-file. An upstream job watcher (not part "
             "of this repo) passes it in.",
    )
    p_tailor.add_argument(
        "--auto", action="store_true",
        help="unattended mode — skip every interactive input() and report success or failure "
             "over Telegram. An upstream job watcher (not part of this repo) calls the tool "
             "with this flag when it sees a new posting.",
    )
    p_tailor.add_argument(
        "--backend", metavar="NAME",
        help="backend for classification: auto (default), ollama, claude-cli, claude-api, "
             "codex-cli, or any name you defined under `backends:` in user_config.yaml",
    )
    p_tailor.add_argument(
        "--heavy-backend", metavar="NAME",
        help="backend that writes the resume sentences (default: auto, which prefers a "
             "frontier model over a local one)",
    )
    p_tailor.add_argument(
        "--keep", action="append", metavar="BULLET_ID",
        help="in minimal mode, never swap this bullet out (repeatable). "
             "Example: --keep example-review-ui",
    )
    p_tailor.add_argument(
        "--no-recruiter", action="store_true",
        help="skip the recruiter review and polish (saves two calls on the strongest model)",
    )
    p_tailor.set_defaults(func=cmd_tailor)

    p_bootstrap = sub.add_parser(
        "bootstrap",
        help="build the experience DB (resume_db.yaml) from your own resume .docx — "
             "run this first",
    )
    p_bootstrap.add_argument("--resume", required=True,
                             help="path to your resume .docx (it needs an EXPERIENCE heading)")
    p_bootstrap.add_argument(
        "--section-heading", default="EXPERIENCE",
        help="text of the heading paragraph the experience blocks start after "
             "(default: EXPERIENCE)",
    )
    p_bootstrap.add_argument(
        "--backend", metavar="NAME",
        help="backend for classification: auto (default), ollama, claude-cli, claude-api, "
             "codex-cli, or any name you defined under `backends:` in user_config.yaml",
    )
    p_bootstrap.set_defaults(func=cmd_bootstrap)

    p_polish = sub.add_parser(
        "polish",
        help="rewrite an existing draft on the heavy backend — fully manual, spends quota",
    )
    p_polish.add_argument("--company", required=True)
    p_polish.add_argument("--role", required=True)
    p_polish.add_argument("--resume",
                          help="path to the source resume .docx "
                               "(default: config.DEFAULT_RESUME_PATH)")
    p_polish.add_argument(
        "--heavy-backend", metavar="NAME",
        help="backend that writes the resume sentences (default: auto)")
    p_polish.add_argument("--profile-json",
                          help="path to the jd_profile.json tailor saved "
                               "(default: guessed from output/)")
    p_polish.set_defaults(func=cmd_polish)

    p_review = sub.add_parser(
        "review",
        help="review an existing draft the way a recruiter would "
             "(with --apply, also rewrite the sentences accordingly)",
    )
    p_review.add_argument("--company", required=True)
    p_review.add_argument("--role", required=True)
    p_review.add_argument("--docx",
                          help=".docx to review (default: the newest draft in "
                               "output/<company>/)")
    p_review.add_argument("--resume",
                          help="the base template .docx "
                               "(default: config.DEFAULT_RESUME_PATH)")
    p_review.add_argument("--apply", action="store_true",
                          help="rewrite the bullets per the review and build a new docx")
    p_review.add_argument("--notify", action="store_true",
                          help="also send the result over Telegram")
    p_review.set_defaults(func=cmd_review)

    p_digest = sub.add_parser("digest",
                              help="send the queued approval requests now, batched")
    p_digest.set_defaults(func=cmd_digest)

    p_doctor = sub.add_parser(
        "doctor", help="check the setup and say what is missing")
    p_doctor.add_argument("--probe", action="store_true",
                          help="also send one real prompt through each backend")
    p_doctor.add_argument("--backend", metavar="NAME", help="backend to probe")
    p_doctor.set_defaults(func=cmd_doctor)

    p_dec = sub.add_parser("decisions",
                           help="summarise the decision journal (✅ / ⏭️ / expired)")
    p_dec.add_argument("--limit", type=int, default=20)
    p_dec.set_defaults(func=cmd_decisions)

    args = parser.parse_args()
    try:
        args.func(args)
    except (errors.SetupError, profile_module.ProfileError,
            llm_client.BackendUnavailable) as e:
        # Setup problems are the user's to fix, so they get a sentence, not a stack
        # trace. Everything else keeps its traceback: that is a bug worth reporting.
        print(f"\n{e}", file=sys.stderr)
        print("\nRun `.venv/bin/python run.py doctor` to check the setup.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
