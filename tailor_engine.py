import json
import re

import ask
import keyword_extractor
import profile as profile_module
import resume_db


def _parse_json_response(raw_text):
    cleaned = raw_text.strip()
    fence_match = re.search(r"```(?:json)?\s*(.*?)\s*```", cleaned, re.DOTALL)
    if fence_match:
        cleaned = fence_match.group(1)
    return json.loads(cleaned)


# The target voice for a rewritten bullet.
#
# These are YOUR sentences, read from profile.yaml's `style_examples`. The model
# is shown them before it edits anything, which sets bullet length, how much
# context a bullet carries, and how numbers are written — the single biggest
# lever on output quality in this pipeline.
#
# With style_examples empty, profile.py substitutes fictional examples. Never
# another real person's bullets: their numbers leak into your résumé.


def reference_style_blob():
    return profile_module.get().style_blob()


TRACK_GUIDANCE = {
    "Consulting": (
        "This JD is most likely screened by a person reading it directly and checking it "
        "against a competency model (leadership, individual impact, proactive "
        "problem-solving). Put one clear competency in each bullet and quantify it, and make "
        "sure the competencies are covered evenly across the bullets you select (do not show "
        "the same competency over and over)."
    ),
    "Tech": (
        "This JD is closer to passing an ATS filter and then getting a short skim from a "
        "recruiter (exact keyword matching plus a check on scope figures). Reflect the "
        "[Repeated JD keywords] in exactly their wording, in the bullets they genuinely fit, "
        "and put the scope figures (users, revenue, team size, budget) early in the sentence."
    ),
    "VC": (
        "This JD is closer to reading judgement than credentials (a feel for sourcing, thesis "
        "thinking, empathy with operators). Present experience that spans several domains "
        "(breadth) as a strength, not a weakness."
    ),
    "PE": (
        "This JD reads with financial rigour and a low tolerance for fluff. Use exact figures "
        "and do not let adjectives stand in for them. Keep the tone conservative."
    ),
    "Startup": (
        "This JD is closest to being read by the founder or practitioner who wrote the posting "
        "themselves. Lead with evidence of building something from zero to one, speed, and "
        "proactive ownership."
    ),
    "General": (
        "There is no clear signal for a specific track. Follow the shared rules above "
        "(quantification, clarity) as the default."
    ),
}

PIVOT_GUIDANCE = """\
[Pivot judgement and the lead element]
Compare the candidate's DB (organisations, roles, keywords) with this JD's role and industry,
judge for yourself whether the candidate is changing role or changing industry on this
application, and pick each bullet's lead element (the information that comes first in the
sentence) by the rules below. In any bullet where rule 5 below (PM bullet style) applies,
rule 5 always wins — apply this table only where it does not conflict with rule 5.
- Role changes only (same industry): lead with How (the transferable skill)
- Industry changes only (same role): lead with the Result, or with Why (a similar situation)
- Both change: How first, then What/Why
- Neither changes (no pivot): lead with the Result
This is a spectrum, not an absolute switch — each bullet may lean differently to suit its own
narrative.
"""


SELECT_PROMPT_TEMPLATE = """\
You are a career coach assembling an MBA internship applicant's résumé for one specific job
posting (JD). Right now you are at the stage of **choosing which experiences go on the page**
— rewriting the sentences happens in the next stage.

[JD ideal-candidate analysis]
{jd_profile_json}

[TRACK strategy] (the approach matched to who actually screens this JD and how — based on
jd_profile.track)
{track_guidance}

[Repeated JD keywords] (words/phrases that genuinely appear several times in the JD text,
descending by frequency)
{keyword_blob}

[Experience DB candidates] (all candidates by section — finished sentences and tags only; the
background material is attached in the next stage)
{db_blob}

Do the following:

1. Section by section, pick the bullets from the DB above that are relevant to this JD. **For
   EXPERIENCE sections headed by a company name (every section except ADDITIONAL — e.g. ACME
   CORP., EXAMPLE CO.), always fill [Max per section] below exactly (the user explicitly
   requires Work Experience to show a fixed number of bullets per company — never cut it down
   to three or any other number on your own).** If that section's DB has at least as many
   candidate bullets as the maximum, you must select exactly the maximum, including candidates
   that are relatively less relevant to this JD as long as they are "not completely unrelated",
   to reach the count — use fewer only in a section where the DB itself has fewer candidates
   than the maximum. **The ADDITIONAL section is the only exception: there you may select a
   variable number up to the maximum, based on JD relevance** (at least one must be included).
   The order of selection matters too: put the strongest bullet for this JD first, in the order
   a recruiter will read them.

[Max per section]
{max_per_section_blob}

[Bullets that must always be included] (include regardless of JD relevance — count them
towards the maximum above)
{pinned_blob}

2. For every DB bullet you did not select, leave a one-line reason in cuts for why it was left
   off this application, grounded in [JD ideal-candidate analysis] / [TRACK strategy] (e.g.
   "far from the competency this JD asks for", "a stronger bullet already covers the same
   competency"). If there were decisions where another choice was nearly as good, leave 1-3 of
   them in close_calls (which bullet it could have been swapped for + why).

3. If there is anything this JD asks for that no bullet in the DB supports well enough, write
   it up as a gap. Do not put a requirement in gaps if the selected bullets cover it in
   substance (e.g. if a requirement is listed as "one of A, B, C" and you are showing one of
   them, it is satisfied).

Respond with the JSON below only (JSON alone, no explanatory prose). The section keys must
match the DB's section names exactly, and each id must be an id that actually exists in the DB
above, written exactly as it appears:
{{
  "sections": {{"section name": ["bullet id", ...], ...}},
  "cuts": {{"section name": [{{"id": "an id you did not select",
                              "reason": "one line on why it was left off"}}, ...], ...}},
  "close_calls": [{{"swap": "what could have been swapped for what", "reason": "why"}}],
  "gaps": [{{"item": "what is missing", "why": "one line on why it matters for this JD"}}]
}}

**cuts.reason / close_calls.reason and the item and why of gaps must be written in English** —
they are explanations a person reads, and gaps are surfaced verbatim in a notification.
"""


REWRITE_PROMPT_TEMPLATE = """\
You are a career coach polishing an MBA internship applicant's résumé for one specific job
posting (JD). Which bullets go on the page has already been decided — your job is to
**rewrite every one of the [Bullets to rewrite] below, one by one**.

[Target style examples] (the versions the applicant hand-edited and settled on, the ones they
were happiest with — do not copy them; treat the density, length, verb choice and clarity of
these sentences as the target level for the bullets you write)
{reference_style_blob}
→ What these examples have in common: (1) the lead verb is different every time
(Built/Designed/Launched/Authored/Re-engineered/Redesigned — the same verb is never
repeated), (2) the causal link between clauses is made explicit with commas, colons and
semicolons, and the same thing is never said twice in different words, (3) sentence length
converges on roughly 190-250 characters including spaces, (4) they are understood in one
pass by someone without the context, with no jargon.

[JD ideal-candidate analysis]
{jd_profile_json}

[TRACK strategy] (the approach matched to who actually screens this JD and how — based on
jd_profile.track)
{track_guidance}

{pivot_guidance}
[Repeated JD keywords] (words/phrases that genuinely appear several times in the JD text,
descending by frequency)
{keyword_blob}
→ Many companies' first screen (an ATS) matches strings literally, with no synonym handling.
   So the list above is not a list of "similar words": you have to use the exact wording given
   here for the match probability to go up. Rule 3 below (no fabricating facts) always wins,
   though — weave a keyword in only where the bullet genuinely fits it, and naturally. Do not
   force one into an unrelated bullet or repeat the same word unnaturally (if it reads
   awkwardly to a human, that alone is a failure — naturalness beats keyword density).

[Bullets to rewrite] (chosen for this JD in the first selection stage — each bullet comes in
two layers)
{db_blob}
{extra_facts_section}
→ How the DB is structured: `Verified core sentence (text)` is the already fact-checked
   skeleton sentence, and the `Background (why)` / `Approach (how)` / `Outcome (impact)`
   beneath it are the raw context the sentence came from. Earlier résumés rewrote only by
   lightly polishing `text`;
   now you keep `text` as the skeleton but treat `why`/`how`/`impact` as source material too,
   picking out the details most meaningful for this JD and assembling the sentence from them
   (see rule 2 below). A fact that is in neither `text` nor `context` still must not be
   invented — write only what is genuinely supported in one of those two places.

Using the information above, do the following:

1. **Rewrite every bullet in [Bullets to rewrite] above, without dropping a single one.** The
   selection is already finished, so you must not re-pick or discard any of them (an id you
   leave out ships with its original sentence, which lowers the quality). Keep the given order
   as well — it is already sorted into the order a recruiter will read.

2. **Assemble the sentence the STAR way (What/Why/How/Impact).** This is not just rewording
   `text`: pick the pieces of the `why`/`how`/`impact` raw context that are most meaningful for
   this JD and reassemble the sentence with them — the style settled on for one internship
   application (see [Target style examples]) was built exactly like this, by pulling only the
   parts of the whole narrative that carry meaning.
   **[Settled by the user — bullets in EXPERIENCE sections headed by a company name must show
   all five elements below, in this order. The ADDITIONAL section is exempt from the ordering
   requirement.]**

   `verb → What → Why → How → Impact`

   - **verb**: start the sentence with a past-tense active verb. Do not start with a
     prepositional phrase, a noun phrase, or someone else's action
     (forbidden examples: "As interim Head of Investment, ...", "Log analysis showed ...",
     "Rivals paid ...").
     **Do not repeat a lead verb within one résumé** — pick a different verb each time, as in
     Launched/Invested/Opened/Built/Established/Designed/Moved/Redesigned/Specified/Developed.
   - **What**: what you built or did. The core output or action from `text`. It comes straight
     after the verb.
   - **Why**: why you did it. Taken from `context.why`. **Do not leave it out** — if you are
     short on characters, cut another modifier instead of deleting the Why ("because a surge in
     pandemic orders had the vendor asking for more headcount", "because the pitch deck's
     top-down market claim could not be trusted", "because capital alone does not differentiate
     a seed investor").
   - **How**: **what difficulty or problem you solved.** Not simply "what you used" but the
     obstacle and the breakthrough ("aligning four internal teams and six outside VCs on a
     programme nobody had run before", "pulling comps from around the world because there was
     no domestic comparable", "designing and building it alone with no engineering support",
     "convincing leadership and engineering to change the roadmap priority"). `context.how` is
     the source.
   - **Impact**: **a quantified result is mandatory.** If the figures are in the DB, keep all
     of them. If the experience genuinely has no figure, write a verifiable state instead
     (e.g. "now live"), and do not invent a number that does not exist.
   - With all five elements a bullet lands at 220-260 characters — that is normal. It only has
     to stay under the cap in rule 3; do not make it short by dropping an element.
   - If any of the [Repeated JD keywords] above genuinely matches that bullet's real
     experience, reflect it naturally, in exactly the same wording.
3. Rules you must keep: do not invent an experience, result or figure the applicant did not
   actually have. Only recombining, re-emphasising and better phrasing of facts inside that
   bullet's `text` and `context` (why/how/impact) is allowed — do not produce a new fact or a
   new bullet outside that range. If [Extra facts gathered] is provided above, you may
   strengthen the content with those facts only.
   - **Length cap (strict)**: no single bullet may exceed 250 characters including spaces.
     It is the same reason the [Target style examples] all sit between 190 and 250 — go past
     this cap and the résumé runs over one page. Even if the DB original is longer, compress
     it to within 250 characters. Ending up longer than the original is always a failure.
{total_budget_blob}
   - **Use of space (strict)**: every line of a résumé is precious space. If a bullet takes two
     lines in the docx, fill both lines; if three, fill three — when the first line is full and
     the second or third is more than half empty, that line is wasted. The 250-character cap is
     a limit for when you overflow, not a claim that shorter is better. Within the lines it
     occupies, carry as much meaningful content (Why/How/Impact) as you can.
   - **Density (strict)**: do not say the same thing twice in different words. For example "held
     the scope" and "blocked the reduction" are the same fact, so put it in one clause only.
     Before adding a clause, ask yourself "is there new information the reader would miss
     without this clause?" — if not, delete it.
5. If the role in [JD ideal-candidate analysis] is in the Product Manager/PM family (per an MBA
   programme career centre's feedback), keep the following in every bullet you rewrite:
   - The lead verb must be one that says "what you built" (Built/Developed/Launched/Shipped/
     Designed/Authored/Re-engineered/Redesigned/Established/Delivered/Engineered/Co-founded and
     the like).
   - That "thing you built" must come in the verb phrase at the very front of the sentence. Do
     not lead with the analysis or the cause as in "Identified X, and built Y"; put the output
     first, as in "Built Y by identifying X" or "Built Y, ...".
   - **Lead-verb variety (strict)**: across all the bullets you rewrite in this response, do not
     use the same lead verb (by root — Built and Build count as the same) more than twice. Do
     not treat "Built" as a default because it is easy; pick the verb that matches each bullet's
     actual action — Designed/Sourced for investing and sourcing, Launched/Authored for
     initiating and spreading something, Re-engineered/Redesigned for improvement and redesign,
     Established/Co-founded for a new organisation or service. Repetition is allowed as an
     exception only when the outputs genuinely overlap and there is no alternative.
6. **Absolutely no em dash (—) (whatever the role; highest priority)**: do not use an em dash
   (—) anywhere in the bullets you rewrite. It reads as AI-written, and the user has banned it.
   To join clauses, rewrite readably with a comma, a full stop (splitting the sentence), a
   semicolon, a colon, or a connecting structure such as "by/after/so" or a participial phrase.
   The en dash (–) in a date range is fixed template wording and is exempt. Even if the input
   bullet's original contains a —, the output must not.
7. Whatever the role, always: write so that a hiring manager reading it for the first time
   understands it in one pass.
   - **Note**: the base template already carries one fixed sentence introducing the company
     directly under each company header in EXPERIENCE (ACME CORP., EXAMPLE CO.) — for example,
     under the EXAMPLE CO. header, "the country's largest mobile platform with 50+ million
     users, spanning messaging, e-commerce, fintech, and content" (this is not a bullet but a
     paragraph of the template itself, so it ships in the output every time). The hiring manager
     reads that description immediately above the bullets — so do not explain again inside an
     ACME CORP./EXAMPLE CO. bullet what the company does (no re-expanding it as "for Example
     Co. (the country's largest messaging platform)"). Only where a header has no such
     introductory line, as in the ADDITIONAL section, and an unfamiliar organisation name
     comes up (a personal project, a small body), add one line saying what it does plus a
     figure that conveys its scale.
   - **No duplication across the whole page**: a résumé is read as one whole page at a glance —
     do not restate in different words a fact that already appears somewhere else (the company
     introduction line, another bullet, EDUCATION, etc.). Before writing a bullet, check
     "is this information already somewhere on this page?", and if it is, leave it out or
     carry it on briefly as a result or a figure only.
   - Award, competition and title names cause a similar problem — with a name like "AI
     Innovation Grand Prize", you cannot tell from the name alone whether it is an external
     award open to a whole industry or an internal contest. If `context` has something that
     conveys the scale (number of entrants, internal versus external), add it briefly in
     parentheses to prevent the misreading (e.g. "winning the firm-wide AI Innovation Grand
     Prize (10 entries)"). If it does not, leave it alone rather than inventing one.
   - Avoid run-ons that string clauses together without a conjunction, and make the relationship
     between clauses (cause/effect, means/end) grammatically explicit.
   - Keep tense and parallel structure consistent, and fix grammatical errors (capitalisation,
     tense and so on) as part of the rewrite.
   - Use plain words. Spell out or delete internal jargon, abbreviations and modifiers that
     someone without the context cannot understand on one reading. Do not add redundant
     restatements that split a figure you already gave into parentheses ("(4,000 of ~15,000)").
8. **Category prefix (ADDITIONAL section)**: if a DB bullet has a `label`, the rewritten
   sentence must start with `"<label>: "` (e.g. "Product Building: Architected a scheduling
   app, ...", "AI Services: Developed ...", "Entrepreneurship: Co-founded ..."). The base
   template's ADDITIONAL section is uniformly in this format, so a missing prefix makes the
   format look broken on the page. Do not change or translate the label wording; use it exactly
   as written in the DB, and start the body after the prefix with a capital letter.
   Do not add a prefix to a bullet with no label (all of the EXPERIENCE sections).

Respond with the JSON schema below only (JSON alone, no explanatory prose). The section keys
must match the section names given above exactly, and each bullet's id must be an id from
[Bullets to rewrite] above, written exactly as it appears (do not invent an id and do not leave
one out):
{{
  "sections": {{
    "section name": [{{"id": "bullet id", "text": "the rewritten text"}}, ...],
    ...
  }}
}}
"""


def _format_total_budget(total_chars_budget):
    """A 250-character cap per bullet is not enough to hold one page — in one measurement
    across 13 generated drafts, the character total of the rewritten sections ran +4 to +339
    characters over the base template (2,552 characters), and all 5 drafts over +150 were
    estimated to overflow the page. The individual bullets were all inside the cap, so the
    problem came from the total creeping up; hence the total budget goes into the prompt
    directly (the budget value is measured from the base template and passed in — change the
    template and it follows automatically)."""
    if not total_chars_budget:
        return ""
    return (
        f"   - **Total budget (strict)**: keep the **combined character count, including spaces, "
        f"of the bullet text you rewrite across the sections above to no more than "
        f"{total_chars_budget} characters**. This value is measured directly from the volume that "
        f"actually fits on one page of the user's base résumé. Even with every individual bullet "
        f"inside 250 characters, exceeding this total runs past one page. If the budget is tight, "
        f"do not reduce the number of bullets (rule 1 wins on counts) — compress by deleting the "
        f"least important clause in each sentence."
    )


def _format_max_per_section(db_sections, max_per_section):
    if not max_per_section:
        return "(no limit)"
    lines = []
    for section in db_sections:
        name = section["name"]
        cap = max_per_section.get(name)
        if cap:
            lines.append(f"- {name}: at most {cap}")
    return "\n".join(lines) if lines else "(no limit)"


def _pinned_ids_by_section(db_sections):
    """Collect the ids marked `bullet.pinned: true`, by section — bullets that must always be
    included regardless of JD relevance (e.g. add-mentoring)."""
    result = {}
    for section in db_sections:
        ids = {b["id"] for b in section["bullets"] if b.get("pinned")}
        if ids:
            result[section["name"]] = ids
    return result


def _format_pinned(db_sections, pinned_by_section):
    if not pinned_by_section:
        return "(none)"
    lines = []
    for section in db_sections:
        ids = pinned_by_section.get(section["name"])
        if not ids:
            continue
        lines.append(f"- {section['name']}: {', '.join(sorted(ids))}")
    return "\n".join(lines) if lines else "(none)"


def select_bullets(backend, jd_profile, db_sections, keyword_counts=None, max_per_section=None,
                   log=print):
    """**Stage 1: choose which bullets go on the page** (only the summary blob is sent — see the
    comment on resume_db.to_select_blob).

    Returns: {"sections": {name: [id, ...]}, "cuts": {...}, "close_calls": [...], "gaps": [...]}
    Ids that are not in the DB or belong to another section are discarded, and pinned ids are
    forced in."""
    valid_section_names = set(resume_db.section_names(db_sections))
    id_to_section = resume_db.id_to_section_name(db_sections)
    pinned_by_section = _pinned_ids_by_section(db_sections)
    retired = {b["id"] for b in resume_db.bullets_by_id(db_sections).values() if b.get("retired")}
    track = (jd_profile or {}).get("track")

    prompt = SELECT_PROMPT_TEMPLATE.format(
        jd_profile_json=json.dumps(jd_profile, ensure_ascii=False, indent=2),
        track_guidance=TRACK_GUIDANCE.get(track, TRACK_GUIDANCE["General"]),
        keyword_blob=keyword_extractor.format_for_prompt(keyword_counts or []),
        db_blob=resume_db.to_select_blob(db_sections),
        max_per_section_blob=_format_max_per_section(db_sections, max_per_section),
        pinned_blob=_format_pinned(db_sections, pinned_by_section),
    )
    result = _parse_json_response(backend.complete(prompt))
    result.setdefault("sections", {})
    result.setdefault("cuts", {})
    result.setdefault("close_calls", [])
    result.setdefault("gaps", [])

    selected = {}
    for name, ids in (result["sections"] or {}).items():
        if name not in valid_section_names:
            log("  [warning] the LLM returned a section name that does not exist; "
                f"ignored: {name!r}")
            continue
        kept = []
        for entry in ids or []:
            # The schema is a list of id strings, but the model sometimes returns {"id": ...}
            # objects, so both are accepted.
            bid = entry.get("id") if isinstance(entry, dict) else entry
            if not isinstance(bid, str) or id_to_section.get(bid) != name:
                log(f"  [warning] ignored an id in section '{name}' that does not exist "
                    f"or belongs elsewhere: {bid!r}")
                continue
            if bid in retired:
                log(f"  [warning] a retired bullet was selected; ignored: {bid}")
                continue
            if bid not in kept:
                kept.append(bid)
        selected[name] = kept

    # Force the pinned bullets in (the safety net for when the LLM ignores the prompt and leaves
    # one out). The cap (run.py._clamp_to_caps) truncates the list from the front, so they always
    # go first.
    for name, pinned_ids in pinned_by_section.items():
        ids = selected.setdefault(name, [])
        for bid in sorted(pinned_ids):
            if bid not in ids:
                log(f"  [pinned] '{bid}' was missing from the LLM selection; forced in")
                ids.insert(0, bid)
            else:
                ids.insert(0, ids.pop(ids.index(bid)))

    result["sections"] = selected
    total = sum(len(v) for v in selected.values())
    log(f"  Stage 1 selection: {total} total " +
        " / ".join(f"{name} {len(ids)}" for name, ids in selected.items()))
    return result


def rewrite_bullets(backend, jd_profile, db_sections, selected, keyword_counts=None,
                    extra_facts=None, total_chars_budget=None, log=print):
    """**Stage 2: rewrite the selected bullets to fit the JD** (only the background material for
    the selected ones is sent).

    Returns: {name: [{"id", "text"}, ...]} — an id with no rewrite keeps the DB original (worse
    sentence quality, but better than the bullet disappearing)."""
    id_to_section = resume_db.id_to_section_name(db_sections)
    bullets_lookup = resume_db.bullets_by_id(db_sections)
    selected_ids = [bid for ids in selected.values() for bid in ids]

    extra_facts_section = ""
    if extra_facts:
        facts_lines = "\n".join(f"- {q}: {a}" for q, a in extra_facts.items())
        extra_facts_section = ("\n[Extra facts gathered] (answered by the user directly; "
                               f"trust them as fact)\n{facts_lines}\n")

    track = (jd_profile or {}).get("track")
    prompt = REWRITE_PROMPT_TEMPLATE.format(
        reference_style_blob=reference_style_blob(),
        jd_profile_json=json.dumps(jd_profile, ensure_ascii=False, indent=2),
        track_guidance=TRACK_GUIDANCE.get(track, TRACK_GUIDANCE["General"]),
        pivot_guidance=PIVOT_GUIDANCE,
        keyword_blob=keyword_extractor.format_for_prompt(keyword_counts or []),
        db_blob=resume_db.to_rewrite_blob(db_sections, selected_ids),
        extra_facts_section=extra_facts_section,
        total_budget_blob=_format_total_budget(total_chars_budget),
    )
    data = _parse_json_response(backend.complete(prompt))

    rewritten = {}
    for name, items in (data.get("sections") or {}).items():
        for item in items or []:
            # The model sometimes breaks the schema and returns a string (the id alone). Blowing
            # up here would throw away the whole generation (the user has already approved it),
            # so that one item is dropped and the loop continues.
            if not isinstance(item, dict):
                log(f"  [warning] a rewrite result had the wrong shape; ignored: {item!r}")
                continue
            bid = item.get("id")
            text = (item.get("text") or "").strip()
            if not bid or not text:
                log(f"  [warning] ignored an item with an empty id/text: {item!r}")
                continue
            if id_to_section.get(bid) != name:
                log(f"  [warning] ignored an id in section '{name}' that does not exist "
                    f"or belongs elsewhere: {bid!r}")
                continue
            rewritten[bid] = text

    cleaned_sections = {}
    for name, ids in selected.items():
        items = []
        for bid in ids:
            text = rewritten.get(bid)
            if not text:
                log(f"  [warning] no rewrite result for {bid}; using the DB original as is")
                text = bullets_lookup[bid]["text"]
            items.append({"id": bid, "text": text})
        cleaned_sections[name] = items
    return cleaned_sections


def generate_section_revisions(backend, jd_profile, db_sections, keyword_counts=None,
                                extra_facts=None, max_per_section=None,
                                total_chars_budget=None, log=print):
    """The engine that selects a variable number of bullets per section from the DB and rewrites
    them — **two calls**.

    They were split apart because the prompt that did selection and rewriting at once had grown
    to 52.7KB, 39KB of which was background material for candidate bullets (ACME CORP. has 14
    candidates and only 4 get selected — the background for the 10 that lose was sent every
    time). Now stage 1 chooses from the summary blob (≈10KB) and stage 2 rewrites with **only
    the background of what was selected** attached (≈20KB). With less load in one call,
    selection quality and sentence quality each get the prompt's full attention.

    db_sections: the return value of resume_db.load_db(). max_per_section: {section_name: max}
    — None prints "no limit" in the prompt (the clamp on the code side is done separately by
    run.py).
    total_chars_budget: cap on the combined character count of the rewritten bullets (measured
    from the base template; see _format_total_budget).

    Returns: {"sections": {name: [{"id", "text"}, ...]}, "cuts", "close_calls", "gaps"}
    """
    selection = select_bullets(backend, jd_profile, db_sections, keyword_counts=keyword_counts,
                               max_per_section=max_per_section, log=log)
    cleaned_sections = rewrite_bullets(backend, jd_profile, db_sections, selection["sections"],
                                       keyword_counts=keyword_counts, extra_facts=extra_facts,
                                       total_chars_budget=total_chars_budget, log=log)
    result = {"cuts": selection.get("cuts") or {}, "close_calls": selection.get("close_calls") or [],
              "gaps": selection.get("gaps") or []}

    # The ADDITIONAL category prefix ("Product Building: ...") is corrected deterministically in
    # code, separately from rule 8 of the prompt — the LLM drops it often.
    result["sections"] = apply_section_labels(cleaned_sections, db_sections)

    # The reasons in cuts are taken on trust from the LLM, but "every DB bullet that was not
    # selected" is computed in code (so the CUTS list is always complete even when the LLM lists
    # only some of them or leaves them out — the DB is the source of truth).
    llm_reasons = {}
    for name, items in (result.get("cuts") or {}).items():
        for item in items or []:
            bid = (item or {}).get("id")
            reason = (item or {}).get("reason", "").strip()
            if bid and reason:
                llm_reasons[bid] = reason

    computed_cuts = {}
    for section in db_sections:
        name = section["name"]
        selected_ids = {item["id"] for item in cleaned_sections.get(name, [])}
        cut_items = []
        for bullet in section["bullets"]:
            bid = bullet["id"]
            if bid in selected_ids:
                continue
            cut_items.append({"id": bid, "reason": llm_reasons.get(bid) or "(no reason given)"})
        if cut_items:
            computed_cuts[name] = cut_items
    result["cuts"] = computed_cuts

    result["close_calls"] = [
        {"swap": (cc or {}).get("swap", "").strip(), "reason": (cc or {}).get("reason", "").strip()}
        for cc in (result.get("close_calls") or [])
        if (cc or {}).get("swap", "").strip()
    ]

    return result


def format_review(result, db_sections):
    """Format THE SET (the selected bullets) / CUTS (what was left out + why) / CLOSE CALLS for a
    person to read. For checking before a build runs — ported from the §5 proposal format in the
    original design notes."""
    id_to_bullet = resume_db.bullets_by_id(db_sections)
    lines = ["=== THE SET (selected bullets) ==="]
    for section in db_sections:
        name = section["name"]
        items = result.get("sections", {}).get(name, [])
        lines.append(f"\n[{name}] {len(items)} bullets")
        for item in items:
            lines.append(f"  - ({item['id']}) {item['text']}")

    lines.append("\n=== CUTS (bullets left out + why) ===")
    cuts = result.get("cuts", {})
    if not any(cuts.values()):
        lines.append("  (nothing was left out — the whole DB was selected)")
    for section in db_sections:
        name = section["name"]
        cut_items = cuts.get(name, [])
        if not cut_items:
            continue
        lines.append(f"\n[{name}]")
        for c in cut_items:
            text = id_to_bullet.get(c["id"], {}).get("text", "")
            lines.append(f"  - ({c['id']}) {text[:70]}\n    -> {c['reason']}")

    close_calls = result.get("close_calls", [])
    if close_calls:
        lines.append("\n=== CLOSE CALLS (decisions worth reconsidering) ===")
        for cc in close_calls:
            reason = f" — {cc['reason']}" if cc.get("reason") else ""
            lines.append(f"  - {cc['swap']}{reason}")

    return "\n".join(lines)


def review_and_confirm(result, db_sections, auto=False):
    """Get a person's approval right before the build. In auto mode (an unattended run triggered
    by an upstream job watcher, not part of this repo) it only prints and always approves (the
    same intent as the fast-track in the original design notes — there is nobody waiting).
    Returns: True (approved, carry on) / False (cancelled)."""
    print("\n" + format_review(result, db_sections))
    if auto:
        print("\n(auto mode — skipping the review confirmation and carrying on)")
        return True
    # Default yes: with no terminal this behaves like --auto. Nothing is submitted
    # anywhere, so carrying on is the safe answer; stopping would throw away the
    # LLM calls already paid for.
    return ask.confirm("\nProceed with this selection? (Y/n): ", default=True)


SHORTEN_PROMPT_TEMPLATE = """\
You are an editor **compressing** a single résumé bullet **to fit one page**.

[Current sentence] ({cur_chars} characters including spaces)
{text}

[Problem]
Rendered in Word, this sentence puts only a word or two on its last line, wasting a whole line.
**Remove at least {target_chars} characters** and that line disappears, fitting the résumé on
one page.

[Compression rules]
1. Do not invent facts. **Adding a figure, proper noun or result that is not in the sentence is
   absolutely forbidden.**
2. Protect the quantified results (the numbers) as far as you can. If there is genuinely nothing
   else to cut you may drop one less important number, but you must not change the value, the
   unit or the subject of a number you keep.
3. Keep the lead verb (the first word of the sentence) and "what you did" exactly as they are.
4. The order of deletion: duplicated modifiers → a clause that states the same fact twice →
   details of the background explanation. "Why you did it" and "the result" survive to the last.
5. No em dash (—). Join clauses with a comma, semicolon, colon or participial phrase.
6. Keep it as the same English sentence, as one sentence. Do not end it with a full stop
   (résumé bullet style).

Output **only the compressed sentence, on one line**. The sentence alone, with no quotes,
explanation or prefix.
"""


def _numbers_in(text):
    return set(re.findall(r"\d[\d,\.%kKmMxX]*", text))


def shorten_bullet(backend, text, target_chars, bullet=None):
    """Rewrite a single bullet at least target_chars shorter (the shorten_fn for pagefit.enforce).

    Anti-fabrication: reject if a number appears that is not in the original (**dropping** a
    number is allowed — that is a loss of information, not a falsehood). Reject if an em dash
    comes in. If the amount cut falls short of 60% of what was asked, reject and let the caller
    (the enforce loop) measure again and retry.
    Returns: the new sentence (str), or None (compression failed — the caller leaves that bullet
    alone)."""
    prompt = SHORTEN_PROMPT_TEMPLATE.format(
        text=text, cur_chars=len(text), target_chars=target_chars,
    )
    raw = (backend.complete(prompt) or "").strip()
    new = raw.strip().strip('"').strip("'").strip()
    new = new.split("\n")[0].strip()
    if not new:
        return None
    if "—" in new:
        print("  [compression rejected] contains an em dash")
        return None
    if not _numbers_in(new) <= _numbers_in(text):
        print("  [compression rejected] a figure not in the original appeared: "
              f"{sorted(_numbers_in(new) - _numbers_in(text))}")
        return None
    saved = len(text) - len(new)
    if saved < target_chars * 0.6:
        print(f"  [compression short] only {saved} characters cut, below the "
              f"{target_chars} required")
        return None
    return new


def apply_section_labels(sections, db_sections):
    """Force any bullet with a `label` (as in ADDITIONAL) to start with the `"<label>: "` prefix.

    The base template's ADDITIONAL section is uniformly formatted with a category prefix
    ("Product Building: ...", "AI Services: ...", "Skills: ..."), but the rewrite prompt alone
    was not enough — the LLM dropped the prefix often (in one run only the Skills line kept it
    and the other three lost it entirely).
    So the code corrects it deterministically, separately from the prompt rule — the DB is the
    source of truth for labels."""
    labels = {b["id"]: b["label"] for b in resume_db.bullets_by_id(db_sections).values()
              if b.get("label")}
    for items in sections.values():
        for item in items:
            label = labels.get(item["id"])
            if not label:
                continue
            text = item["text"].strip()
            head = text.split(":", 1)[0]
            if head.lower() == label.lower():
                item["text"] = f"{label}:{text.split(':', 1)[1]}".strip()
            elif len(head) <= 28 and head.lower() in {l.lower() for l in labels.values()}:
                # If the LLM attached a different category name, replace it with the DB label.
                item["text"] = f"{label}: {text.split(':', 1)[1].strip()}"
            else:
                item["text"] = f"{label}: {text}"
    return sections


def run_gap_qa(gaps):
    """Print the gap list and take an answer for each one from the user (Enter = skip).
    Interactive only — never called in auto mode (an unattended run triggered by an upstream job
    watcher, which is not part of this repo), because there is nobody there to answer and it
    would simply hang (see the --auto branch in run.py)."""
    if not gaps:
        return {}
    print("\n[What is missing against the JD]")
    answers = {}
    for g in gaps:
        item = g.get("item", "").strip()
        why = g.get("why", "").strip()
        if not item:
            continue
        print(f"\n- {item}")
        if why:
            print(f"  (why: {why})")
        ans = ask.line("  Answer if you have a relevant experience or fact (Enter to skip): ")
        if ans:
            answers[item] = ans
    return answers
