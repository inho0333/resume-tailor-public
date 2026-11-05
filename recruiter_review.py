"""Judge the generated draft **the way a recruiter for that company and that role would, and
rewrite the sentences according to that judgement.**

## Why this step is separate

The existing rewrite (`tailor_engine.generate_section_revisions`) is the job of "reassembling
facts from the database to fit the JD". The quality bar its prompt enforces is the style you
fixed by hand (length, verbs, density), and **who is reading the page enters only as a single
TRACK line.** Real screening does not work that way:

  - A single MBA internship posting draws dozens to hundreds of applicants from the same
    schools with the same backgrounds. The recruiter skims one page for 6-8 seconds and splits
    the pile three ways (advance / hold / reject).
  - What is actually read in those six seconds is the lead verbs, the first 7-8 words, the size
    and unit of the numbers, and how recognizable the company names are. Impact hidden at the
    end of a sentence is effectively not read.
  - The same fact reads as ordinary written as "did X" and passes written as "moved X"
    (scope, dollars, users).

So the pipeline gets a step that **takes the same facts and rewrites them into sentences that
appeal to someone at that company.** It is split into two calls in exactly the order you asked
for — (1) a critical review (what is weak and why), (2) a polish that follows that review. The
review is kept in the report and in
`output/<company>/<company>_<role>_recruiter_review.json` so a human can check the basis.

## The line of defence against invented facts (the code verifies it)

Polish rewrites sentences to raise quality, so it is the point where the pressure to exaggerate
is strongest. Prompt prohibitions alone were not enough (measured once: exaggerations from an
old document had worked their way into the database itself), so the code verifies the result:

  - The allowed set of numbers = numbers in the original sentence plus numbers appearing in the
    database `text`/`context` (why/how/impact). If any other number is created, that bullet is
    **discarded and the original kept** (removing a number is allowed).
  - An em dash (—) in the output is rejected (your instruction; validate.py also treats it as
    a FAIL).
  - Over the length cap (260 chars), an id that is not in the database, or an id belonging to
    another section is rejected.
  - Label prefixes are re-applied afterwards with `tailor_engine.apply_section_labels`.
"""

import json
import os
import re

import llm_utils
import pagefit
import profile as profile_module
import resume_db
import tailor_engine

# Rewrite prompt cap (250 chars) plus slack. Over this, the polish is rejected.
MAX_BULLET_CHARS = 260


CRITIQUE_PROMPT_TEMPLATE = """\
You are the **recruiter and hiring manager who personally owns hiring** for the "{role}"
position at {company}. You are running the first screening pass over the pile of
{applicant_phrase} resumes for this position.

[The real conditions of this screen — you must read on these premises]
- Dozens to hundreds of {peer_pool_phrase} apply to this same position. Many carry
  well-known logos and a directly relevant title.
- You spend 6-8 seconds on one resume. Only the lead verbs, the first 7-8 words of each bullet,
  the size and unit of the numbers, and the recognizability of the organization names are
  effectively read. Assume impact hidden at the end of a sentence is not read.
- You have no reason to be kind to this applicant. Do not write praise. Your job is to find
  reasons to reject.
- This is the applicant's background: {career_summary}. Where it does not line up with what
  this position screens for, say so honestly, **separating what can be fixed in the sentences
  from what cannot.**

[Job posting analysis]
{jd_profile_json}

[Excerpt of the posting]
{jd_excerpt}

[Phrases actually repeated in the JD] (descending frequency — the exact words a first-pass ATS
matches as strings)
{keyword_blob}
{research_block}
[Bullets of the resume about to be submitted] (by section, with ids)
{bullets_blob}

[The full range of facts this applicant may use] (no fact outside this exists —
never go beyond this range when you write improvement instructions)
{facts_blob}

Read the above and judge the following.

1. **6-second scan result**: what stays in your head after skimming this page for six seconds,
   and your first reaction.
2. **Screening score (1-10)**: 10 is "interview, no question", 7 is "worth advancing", 5 is
   "borderline", 3 or below is "reject". Score against the real applicant pool for this
   position. Do not be generous.
3. **What snagged you (top_concerns)**: 2-4 things you would write down as reasons to reject.
   One line each, **pointing at a specific sentence or phrase** (no abstract remarks like
   "the impact is weak").
4. **Per-bullet review**: one bullet at a time, judge the following against this JD.
   - `problem`: what a recruiter sees as wrong (weak lead / numbers buried at the end / space
     spent on detail irrelevant to this JD / scope unknowable / not translated into this
     company's context / no problem).
   - `fix_instruction`: a concrete instruction for **how** that sentence should be fixed. You
     may only work inside [the full range of facts] above. An instruction to invent a figure,
     customer name or technology that does not exist is discarded.
   - `priority`: 1 (must fix) / 2 (good to fix) / 3 (leave as is).
5. **Signals that are not visible (missing_signal)**: things a person applying to this JD must
   show on the resume that are nowhere on this page. Exclude anything the applicant simply does
   not have, and write down **only what they have but the sentences do not reveal** (find the
   basis in [the full range of facts] above).
6. **ATS misses (ats_misses)**: phrases from [Phrases actually repeated in the JD] that match
   this applicant's real experience but are not used in the bullet sentences. Do not include
   ones that do not match.

Respond with the JSON below only (JSON only, no prose). Write every explanation in **English**:
{{
  "first_impression": "two sentences of your first reaction after the 6-second scan",
  "screening_score": an integer from 1 to 10,
  "verdict": "advance" | "borderline" | "reject",
  "competitive_context": "one or two sentences on where this resume sits in the pool",
  "top_concerns": ["one line per thing that snagged you", ...],
  "bullet_feedback": [
    {{"id": "bullet id", "priority": 1|2|3, "problem": "the problem",
      "fix_instruction": "how to fix it"}}
  ],
  "missing_signal": ["a signal they have but do not reveal"],
  "ats_misses": ["a JD phrase that matches but is unused"]
}}
"""


POLISH_PROMPT_TEMPLATE = """\
You are an editor who **rewrites the resume bullets of {applicant_phrase} according to
recruiter feedback.** A recruiter for the "{role}" position at {company} has just put this
resume through a first screen and left the review below.

[Recruiter review]
Overall impression: {first_impression}
Score: {screening_score}/10 ({verdict})
Competitive context: {competitive_context}
What snagged them:
{concerns_blob}
Signals that are not visible:
{missing_blob}
JD phrases to reflect (ATS):
{ats_blob}

[Bullets to fix, with instructions]
{feedback_blob}

[The full range of facts these bullets may use] (no fact outside this exists)
{facts_blob}

[Target style examples] (the versions the applicant fixed by hand — the target level of
density, length and verb choice)
{reference_style_blob}

Rewrite each bullet according to the instructions above. The following are constraints
**whose violation makes the code discard the result**:

1. **Never invent facts (highest priority).** Do not create figures, proper nouns, outcomes or
   technologies that are not in [the full range of facts] above. You may only use numbers from
   that range, and you may not change their value, unit or subject. If the recruiter's
   instruction demands a fact that is not there, **ignore** that instruction and fix only the
   structure of the sentence.
2. **Fixing the lead is the core of this.** Within the first 7-8 words of each sentence must
   come "what was built or moved" and, where possible, the scale. Start with a past-tense
   active verb; do not start with a prepositional phrase, an analysis, or someone else's
   action. Do not use the same lead verb more than twice in one resume.
3. **Keep the structure in the order `verb -> What -> Why -> How (the difficulty solved) ->
   Impact (quantified)`.** Sections headed by a company name (ACME CORP., EXAMPLE CO., etc.)
   must show all five elements, and you may not delete the Why to make the sentence shorter.
   The ADDITIONAL section is exempt from this ordering requirement.
4. **Length**: one bullet is at most 250 characters including spaces. {budget_line}
5. **Never use an em dash (—).** To join clauses use commas, semicolons, colons, participial
   phrases, or by/after/so.
6. In the ADDITIONAL section, if a label prefix ("Product Building: " etc.) is in the original,
   keep it exactly.
7. Do not restate a fact that is already in another bullet or in a company description line.
   Do not say the same thing twice in different words.
8. For a bullet you judge does not need fixing, **leave it out of the output** (the original is
   used as is). If changing it makes it worse, not changing it is the right answer.
9. **Do not delete a bullet or merge it into another bullet.** The per-section counts are
   already fixed, and an empty text or an omission only results in the original staying in that
   slot (measured: one bullet was emptied with the note that it had been "absorbed into another
   bullet", and the same content shipped twice). If you see duplication, solve it by rewriting
   the overlapping clauses **within each bullet** so they differ.
10. The applicant has fixed the format of the sentences below, so **never edit them** (do not
   even put them in the output):
   {protected_blob}

Respond with the JSON below only (JSON only, no prose). Write `notes` in English, and bullet
text in English:
{{
  "sections": {{
    "section name": [{{"id": "bullet id", "text": "the rewritten sentence"}}, ...]
  }},
  "notes": ["one line on what you changed and why", ...]
}}
"""


def _facts_blob(ids, db_sections):
    """The fact range for review/polish — only the database original and context of the target
    bullets. Why the whole database (39KB) is not put in again: the selection is already done by
    this step, and prompt bloat was an existing problem in this system (it had grown to 52KB per
    run)."""
    lookup = resume_db.bullets_by_id(db_sections)
    lines = []
    for bid in ids:
        b = lookup.get(bid)
        if not b:
            continue
        lines.append(f"- id={bid}")
        lines.append(f"  verified core sentence: {b['text']}")
        ctx = b.get("context") or {}
        for key, label in (("why", "background"), ("how", "how it was done"),
                           ("impact", "outcome")):
            if ctx.get(key):
                lines.append(f"  {label}({key}): {ctx[key]}")
    return "\n".join(lines) or "(none)"


def _bullets_blob(sections):
    lines = []
    for name, items in sections.items():
        lines.append(f"### {name}")
        for item in items:
            lines.append(f"- id={item['id']} ({len(item['text'])} chars): {item['text']}")
    return "\n".join(lines)


def _allowed_numbers(original_text, bullet):
    """The set of numbers usable in this bullet = the original + the database text + the whole
    context. Polish has to be able to pull a number up out of the context (that is its main way
    of improving a sentence), so the set is not limited to the original. Any number outside it
    is treated as invented and rejected."""
    blob = original_text + " " + (bullet or {}).get("text", "")
    ctx = (bullet or {}).get("context") or {}
    for key in ("why", "how", "impact"):
        if ctx.get(key):
            blob += " " + str(ctx[key])
    return tailor_engine._numbers_in(blob)


def _norm_numbers(nums):
    """Normalization for comparison — the 25 in "25 hours" and the 25,000 in "25,000" are
    different numbers, but values differing only in commas or the case of the unit (2.5M vs
    2.5m) count as the same number."""
    return {n.replace(",", "").lower().rstrip(".") for n in nums}


def review(backend, company, role, jd_profile, sections, db_sections, jd_text="",
           keyword_counts=None, log=print):
    """(1) Critical review from the recruiter's point of view. Returns: the review dict
    (None on failure).

    Pass a backend built on the heavier model (`config.POLISH_MODEL`) as `backend`."""
    import keyword_extractor

    ids = [item["id"] for items in sections.values() for item in items]
    research = (jd_profile or {}).get("company_research") or {}
    research_block = ""
    if research.get("notable") and research.get("notes"):
        research_block = ("\n[Recent company news] (context for the screen)\n"
                          f"{research['notes']}\n")

    prof = profile_module.get()
    prompt = CRITIQUE_PROMPT_TEMPLATE.format(
        company=company, role=role,
        applicant_phrase=prof.applicant_phrase,
        peer_pool_phrase=prof.peer_pool_phrase,
        career_summary=prof.career_summary or "not stated in profile.yaml",
        jd_profile_json=json.dumps(
            {k: v for k, v in (jd_profile or {}).items() if k != "company_research"},
            ensure_ascii=False, indent=2),
        jd_excerpt=(jd_text or "").strip()[:3000] or "(no raw text)",
        keyword_blob=keyword_extractor.format_for_prompt(keyword_counts or []),
        research_block=research_block,
        bullets_blob=_bullets_blob(sections),
        facts_blob=_facts_blob(ids, db_sections),
    )
    raw = backend.complete(prompt)
    data = tailor_engine._parse_json_response(raw)

    score = data.get("screening_score")
    try:
        data["screening_score"] = max(1, min(10, int(score)))
    except (TypeError, ValueError):
        data["screening_score"] = None
    if data.get("verdict") not in ("advance", "borderline", "reject"):
        data["verdict"] = None
    data["top_concerns"] = [str(c).strip() for c in (data.get("top_concerns") or []) if str(c).strip()][:4]
    data["missing_signal"] = [str(c).strip() for c in (data.get("missing_signal") or []) if str(c).strip()][:4]
    data["ats_misses"] = [str(c).strip() for c in (data.get("ats_misses") or []) if str(c).strip()][:8]

    valid_ids = set(ids)
    feedback = []
    for fb in data.get("bullet_feedback") or []:
        bid = (fb or {}).get("id")
        if bid not in valid_ids:
            continue
        try:
            priority = int(fb.get("priority", 2))
        except (TypeError, ValueError):
            priority = 2
        feedback.append({
            "id": bid,
            "priority": max(1, min(3, priority)),
            "problem": str(fb.get("problem") or "").strip(),
            "fix_instruction": str(fb.get("fix_instruction") or "").strip(),
        })
    data["bullet_feedback"] = feedback

    log(f"  Recruiter review: {data['screening_score']}/10 ({data['verdict']}) — "
        f"{sum(1 for f in feedback if f['priority'] <= 2)} bullets to fix")
    for c in data["top_concerns"]:
        log(f"    [concern] {c}")
    return data


def polish(backend, company, role, review_data, sections, db_sections,
           total_chars_budget=None, log=print):
    """(2) Rewrite the bullets according to the review. Returns: (new sections, list of changes).

    Changes: [{"id", "before", "after", "before_chars", "after_chars"}] — for the report/audit.
    A rejected polish does not appear in the change list and the original is kept."""
    id_to_section = {item["id"]: name for name, items in sections.items() for item in items}
    text_by_id = {item["id"]: item["text"] for items in sections.values() for item in items}
    lookup = resume_db.bullets_by_id(db_sections)

    # Lines whose format you fixed by hand (Skills:/Languages:) are **excluded** from polish.
    # Measured: when the recruiter noted that "the AI depth is not visible", polish rebuilt the
    # Skills line from 125 to 240 chars and even reordered the items. That line's "capability
    # (tool)" order and composition are fixed by your instruction (the same reason, and the same
    # list, as pagefit's PROTECTED_PREFIXES).
    protected_ids = {bid for bid, text in text_by_id.items() if pagefit.is_protected(text)}

    targets = [f for f in (review_data.get("bullet_feedback") or [])
               if f["priority"] <= 2 and f["id"] not in protected_ids]
    if not targets:
        log("  Polish skipped — the recruiter named no sentence to fix")
        return sections, []

    feedback_lines = []
    for f in sorted(targets, key=lambda f: f["priority"]):
        feedback_lines.append(
            f"- id={f['id']} [section: {id_to_section.get(f['id'], '?')}] "
            f"(priority {f['priority']})\n"
            f"  current sentence: {text_by_id.get(f['id'], '')}\n"
            f"  recruiter's objection: {f['problem']}\n"
            f"  direction to fix: {f['fix_instruction']}"
        )

    budget_line = ""
    if total_chars_budget:
        budget_line = (f"The total character count of the bullets being rewritten must stay "
                       f"within a budget of {total_chars_budget} characters, so if you grow one "
                       f"sentence, shorten a clause elsewhere.")

    protected_blob = "\n".join(
        f"   - {text_by_id[bid]}" for bid in sorted(protected_ids)) or "   - (none)"

    prompt = POLISH_PROMPT_TEMPLATE.format(
        company=company, role=role, protected_blob=protected_blob,
        applicant_phrase=profile_module.get().applicant_phrase,
        first_impression=review_data.get("first_impression") or "(none)",
        screening_score=review_data.get("screening_score"),
        verdict=review_data.get("verdict"),
        competitive_context=review_data.get("competitive_context") or "(none)",
        concerns_blob="\n".join(f"- {c}" for c in review_data.get("top_concerns") or []) or "- (none)",
        missing_blob="\n".join(f"- {c}" for c in review_data.get("missing_signal") or []) or "- (none)",
        ats_blob=", ".join(review_data.get("ats_misses") or []) or "(none)",
        feedback_blob="\n".join(feedback_lines),
        facts_blob=_facts_blob([f["id"] for f in targets], db_sections),
        reference_style_blob=tailor_engine.reference_style_blob(),
        budget_line=budget_line,
    )
    raw = backend.complete(prompt)
    data = tailor_engine._parse_json_response(raw)

    applied = []
    new_sections = {name: [dict(item) for item in items] for name, items in sections.items()}
    for name, items in (data.get("sections") or {}).items():
        if name not in new_sections:
            log(f"  [polish rejected] no such section: {name!r}")
            continue
        for item in items or []:
            bid = (item or {}).get("id")
            new_text = llm_utils.normalize_typography((item or {}).get("text", "")).strip()
            if not bid or not new_text:
                continue
            if id_to_section.get(bid) != name:
                log(f"  [polish rejected] {bid}: not in the database, or in another section")
                continue
            before = text_by_id[bid]
            if new_text == before:
                continue
            if bid in protected_ids:
                log(f"  [polish rejected] {bid}: a line whose format you fixed (Skills/Languages)")
                continue
            if "—" in new_text:
                log(f"  [polish rejected] {bid}: contains an em dash")
                continue
            exotic = sorted({ch for ch in new_text if ch in llm_utils.NON_ASCII_DASHES})
            if exotic:
                log(f"  [polish rejected] {bid}: non-ASCII dash "
                    f"({', '.join('U+%04X' % ord(ch) for ch in exotic)})")
                continue
            if len(new_text) > MAX_BULLET_CHARS:
                log(f"  [polish rejected] {bid}: {len(new_text)} chars, over the cap "
                    f"({MAX_BULLET_CHARS} chars)")
                continue
            allowed = _norm_numbers(_allowed_numbers(before, lookup.get(bid)))
            got = _norm_numbers(tailor_engine._numbers_in(new_text))
            if not got <= allowed:
                log(f"  [polish rejected] {bid}: figures outside the fact range appeared "
                    f"{sorted(got - allowed)}")
                continue
            for target in new_sections[name]:
                if target["id"] == bid:
                    target["text"] = new_text
                    break
            applied.append({"id": bid, "before": before, "after": new_text,
                            "before_chars": len(before), "after_chars": len(new_text)})

    # Polish can swallow a label prefix, so they are re-applied here (the database is the source
    # of truth).
    new_sections = tailor_engine.apply_section_labels(new_sections, db_sections)

    for a in applied:
        log(f"  [polish] {a['id']}: {a['before_chars']} chars → {a['after_chars']} chars")
        log(f"      before: {a['before']}")
        log(f"      after : {a['after']}")
    if not applied:
        log("  Every polish result was rejected or identical, so the originals are kept")

    # If the total goes over budget, **revert the edits that grew the most, first**. Measured:
    # polish grew every bullet by 10-15 chars, the total reached 2,795 chars against a budget of
    # 2,598, and the page estimate flipped to a +2.0 line FAIL. pagefit's widow compression only
    # fixes "one or two words on the last line", so it cannot undo this kind of across-the-board
    # growth — the budget has to be held here. Edits that shrank a bullet are left alone.
    if total_chars_budget:
        def _total():
            return sum(len(i["text"]) for items in new_sections.values() for i in items)

        grew = sorted((a for a in applied if a["after_chars"] > a["before_chars"]),
                      key=lambda a: a["after_chars"] - a["before_chars"], reverse=True)
        for a in grew:
            if _total() <= total_chars_budget:
                break
            for items in new_sections.values():
                for item in items:
                    if item["id"] == a["id"]:
                        item["text"] = a["before"]
            applied.remove(a)
            log(f"  [reverted, over budget] {a['id']}: cancelled a "
                f"+{a['after_chars'] - a['before_chars']} char edit")
        total = _total()
        if total > total_chars_budget:
            log(f"  [warning] even after reverting, the total is {total} chars > budget "
                f"{total_chars_budget} chars — the one-page measurement (pagefit) will "
                f"edit further")
        else:
            log(f"  After polish: {total} chars / budget {total_chars_budget} chars")

    for note in (data.get("notes") or [])[:6]:
        log(f"  [polish note] {note}")
    return new_sections, applied


def run(review_backend, company, role, jd_profile, sections, db_sections, jd_text="",
        keyword_counts=None, total_chars_budget=None, polish_enabled=True, log=print):
    """Review -> (optional) polish. Returns: (new sections, review_data).

    If either step fails, **the original sections are returned unchanged** — this step is a
    quality improvement, and its failure must not block the draft itself (the same fail-soft
    principle as company_research). review_data gets the `applied_count` the report uses.

    With polish_enabled=False (minimal mode, `config.RECRUITER_POLISH`) it **only reviews** and
    does not touch the sentences — the objections reach a human through the report, and the
    human decides on the edits. Measured once: automatic polish compressed the canonical
    sentences into telegram style ("held to ~20%/~5% vs 60-70% claims") and produced, by your
    judgement, the worst output of the whole system."""
    try:
        log("\nReviewing from the recruiter's point of view (heavy model)...")
        review_data = review(review_backend, company, role, jd_profile, sections, db_sections,
                             jd_text=jd_text, keyword_counts=keyword_counts, log=log)
    except Exception as e:
        log(f"  [recruiter review failed (skipped)] {e}")
        return sections, None

    if not polish_enabled:
        log("  Review only (no automatic sentence edits) — the objections go out in the report.")
        review_data["applied_count"] = 0
        review_data["applied"] = []
        review_data["polish_skipped"] = True
        return sections, review_data

    try:
        log("\nPolishing sentences according to the review (heavy model)...")
        new_sections, applied = polish(review_backend, company, role, review_data, sections,
                                       db_sections, total_chars_budget=total_chars_budget, log=log)
    except Exception as e:
        log(f"  [polish failed (skipped)] {e}")
        review_data["applied_count"] = 0
        review_data["applied"] = []
        return sections, review_data

    review_data["applied_count"] = len(applied)
    review_data["applied"] = applied
    return new_sections, review_data


def save_audit(out_dir, company, role, review_data, safe_name):
    """Keep the raw review plus the before/after in a file — only two lines reach the report, so
    this file is where you look for the basis. The path is the same company folder as the
    docx."""
    if not review_data:
        return None
    path = os.path.join(out_dir, f"{safe_name(company)}_{safe_name(role)}_recruiter_review.json")
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(review_data, f, ensure_ascii=False, indent=2)
        return path
    except OSError as e:
        print(f"  [warning] could not save the recruiter review (skipped): {e}")
        return None


# ── Reverse mapping, used when reviewing an already-generated docx ───────────

def _tokens(text):
    return {w for w in re.findall(r"[a-zA-Z0-9%$]+", (text or "").lower()) if len(w) > 2}


def match_docx_bullets(doc, db_sections, min_overlap=0.18):
    """Map the bullet paragraphs of an already-saved docx back to database ids — a fallback that
    lets `run.py review` (manual review) also review older output that has no `result.json`.

    Each paragraph is attached to the most similar database bullet by word overlap (Jaccard).
    Because the rewrite is a reassembly of database sentences, proper nouns and numbers survive
    in it, and measurement showed the scores separate well enough. A paragraph below the
    threshold is dropped (a wrong attachment would have polish verified against the wrong fact
    range — dropping is the safe side).
    Returns: {section name: [{"id", "text"}]}"""
    import resume_writer

    headers = {s["header_text"] for s in db_sections}
    out = {}
    for section in db_sections:
        try:
            _, paragraphs = resume_writer.find_section_block(
                doc, section["header_text"], stop_texts=headers)
        except (ValueError, KeyError):
            continue
        candidates = {b["id"]: _tokens(b["text"] + " " + json.dumps(b.get("context") or {}, ensure_ascii=False))
                      for b in section["bullets"]}
        used = set()
        items = []
        for p in paragraphs:
            text = p.text.strip()
            if not text:
                continue
            ptokens = _tokens(text)
            best_id, best_score = None, 0.0
            for bid, btokens in candidates.items():
                if bid in used or not btokens:
                    continue
                score = len(ptokens & btokens) / max(1, len(ptokens | btokens))
                if score > best_score:
                    best_id, best_score = bid, score
            if best_id and best_score >= min_overlap:
                used.add(best_id)
                items.append({"id": best_id, "text": text})
            else:
                print(f"  [warning] no database id found, excluded from the review: {text[:60]}…")
        if items:
            out[section["name"]] = items
    return out
