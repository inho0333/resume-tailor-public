"""Handles a Telegram button press (✅ Make the resume / ⏭️ Skip) — when
`bot_listener.py` receives a `jw:make:<id>` / `jw:skip:<id>` callback it calls into
this module.

Policy change: **nothing expensive runs before consent** (user request — "too many
drafts are being made; do the real generation only after I agree"). So `handle_make`
calls `run.generate_from_pending()`, which does company research → section selection /
rewriting → recruiter review and polish → docx right there (several minutes; meanwhile
the listener uses one thread per callback, so other buttons are not blocked).

Added later:
  - **Global serialization** (genlock): one generation at a time. When the user presses
    buttons back to back (measured: two of them 17 seconds apart) the two pipelines use
    the same single Word instance and the same LLM session budget at once. The full
    reasoning is in the genlock.py comments.
  - **Decision journal**: ✅/⏭️ are booked into decisions.jsonl so the upstream filter
    and the pre-fit assessment can learn from them.
  - On failure the report carries a **retry button** (the record is left in place).

Older records (pending entries from before the policy change) carry the generation
output in `result` — only then is the old render-only path used, with no second LLM call.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import docx

import confirm_state
import decisions
import genlock
import notify
import resume_db
import run


def _retry_keyboard(short_id):
    return [[("🔁 Retry", f"jw:make:{short_id}")]]


def handle_make(short_id):
    record = confirm_state.get(short_id)
    if not record:
        notify.alert(f"⚠️ Resume request not found (expired, or a duplicate click): {short_id}")
        return

    if not record.get("result"):
        # The normal path since the policy change — nothing expensive runs before consent,
        # so the record holds no generation output. Company research, selection, rewriting,
        # review and docx all happen now, for the first time (several minutes).
        if not confirm_state.try_claim(short_id):
            # Already generating. Press the button twice and the listener starts two
            # threads and the whole LLM round trip runs twice — the second stops here.
            notify.silent(f"[confirm] already generating — duplicate click ignored: {short_id}")
            return
        try:
            label = f"{record.get('company')} · {record.get('role')}"
            with genlock.GenerationLock(
                label=label,
                on_wait=lambda holder: notify.silent(
                    f"[confirm] another generation is in flight — queueing ({holder}): {label}"),
            ):
                ok = run.generate_from_pending(record, short_id=short_id)
        except genlock.LockBusy as e:
            confirm_state.release_claim(short_id)
            notify.alert(f"⚠️ {notify.esc(str(e))}\nPlease press again shortly: "
                         f"{notify.esc(record.get('company'))} · {notify.esc(record.get('role'))}")
            return

        if ok:
            decisions.record("make", record)
            confirm_state.discard(short_id)
        else:
            # On failure the record is **kept** — deleting it leaves no way to retry. One
            # posting disappeared exactly this way: generation failed 1 hour 26 minutes
            # after consent and the record went with it. If the rewriting stage did
            # succeed, its output is saved on the record (run.generate_from_pending), so a
            # retry starts at rendering with no further LLM call.
            confirm_state.release_claim(short_id)
            notify.silent(
                f"[confirm] generation failed — pending kept, press again to retry: {short_id}")
        return

    # A record that has `result` — either an old pending entry, or state saved for a retry
    # after a failure that happened once rewriting was already done. Either way, all that
    # is left is rendering, with no second LLM call.
    company, role = record["company"], record["role"]
    try:
        with genlock.GenerationLock(label=f"render {company} · {role}"):
            _render_only(record, short_id)
        decisions.record("make", record)
        confirm_state.discard(short_id)
    except Exception as e:
        notify.alert(f"<b>⚠️ Resume generation after confirmation failed</b>\n"
                     f"{notify.esc(company)} · {notify.esc(role)}\n{notify.esc(str(e))}")


def _render_only(record, short_id):
    """Take the saved generation output (`result`) through docx rendering → the measured
    one-page check → upload → report, and nothing more."""
    company, role = record["company"], record["role"]
    resume_path = record["resume_path"]
    if not os.path.exists(resume_path):
        raise FileNotFoundError(f"Resume template not found: {resume_path}")
    doc = docx.Document(resume_path)
    db_sections = resume_db.load_db(run.config.DB_PATH)

    report = notify.Report(company, role, url=record.get("url"))
    report.set_eligibility(record["eligibility"])
    research = (record["jd_profile"] or {}).get("company_research")
    if research:
        report.set_research(research)
    result = record["result"]
    report.set_gaps(result.get("gaps"))
    if record.get("recruiter_review"):
        report.set_recruiter(record["recruiter_review"])

    run._finalize_and_send(
        doc, result, db_sections, company, role, resume_path,
        record.get("jd_text", ""), record["jd_profile"], report,
        # Old records go through the measured one-page check and fixes too — shortening
        # uses the same heavy backend the generation does.
        shorten_backend=run.llm_client.get_heavy_backend(),
    )
    report.send()


def handle_skip(short_id):
    record = confirm_state.get(short_id)
    confirm_state.discard(short_id)
    if record:
        decisions.record("skip", record)
        notify.silent(f"[confirm] skipped: {record['company']} · {record['role']}")
        notify.info(f"⏭️ Skipped: {notify.esc(record['company'])} · "
                    f"{notify.esc(record['role'])}")
