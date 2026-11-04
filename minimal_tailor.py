"""Tailoring to a JD as the **smallest possible edit** — it only reworks wording inside the
base résumé's own sentences, and swaps at most two bullets.

## Why this module exists

Before it, the pipeline **re-picked and completely rewrote** the bullets out of
`resume_db.yaml` for every posting (`tailor_engine.generate_section_revisions` →
`recruiter_review.polish`). The result was, by the owner's judgement, about as bad as it
gets. Sentences that actually shipped:

    Built a bottom-up model at a $500M AUM firm sizing customers x willingness-to-pay
    (labor saved, spend on alternatives) x capture rate, held to ~20%/~5% vs 60-70% claims;
    invested $4.2M across 40 startups (18 AI-driven), 7 with follow-on from a top-tier fund.

You cannot tell what it is saying (`held to ~20%/~5% vs 60-70% claims`), and cramming facts
into a 250-character budget pushed the Why and the Impact out, so there is no impact either.
The cause is structural — generating the sentences fresh for every posting means (1) the
sentence quality that was built up by hand is thrown away each time, (2) the pressure to
compress information inside a character budget produces telegraphic notation such as "x"
chaining, `vs` and tildes, and (3) the recruiter polish step compresses it once more.

So the standard changed: **the base résumé is the source of truth.** Tailoring to a JD is
only
  - word/phrase substitution (an ATS keyword, in the same wording, in a sentence it genuinely
    fits), and
  - swapping 1-2 bullets (bringing in an experience from the DB that sits closer to the JD)
and nothing else. Sentence structure (verb + What + Why + How + Impact) and length follow the
base résumé.

## What the code enforces (it is not left to the prompt)

Prompt rules were already being ignored by the recruiter polish step: only the em dash ban
and the no-invented-numbers ban were actually enforced in code, and qualitative rules like
"a sentence a reader can understand" were honoured by nobody. So this module checks every
edit in code, and an edit that does not pass is **dropped silently, keeping the base
sentence**:

  1. Word retention at or above `MIN_WORD_OVERLAP` — below that it is a rewrite, not an edit.
  2. Length growth at most `MAX_LEN_GROWTH` chars + the section total no higher than the base
     total — one page is then guaranteed structurally.
  3. No new numbers (figures absent from the base sentence and from the DB `text`/`context`) —
     the same principle as the existing anti-fabrication check.
  4. No telegraphic notation — `A x B x C` chaining, `vs`, `~`, more than one set of
     parentheses, more than one semicolon. This was the actual shape of "I cannot tell what
     this says".
  5. The figures in the last clause (Impact) survive — if an edit drops every number the base
     sentence had, it is rejected.
  6. No em dash (the existing rule), and the Skills/Languages lines are excluded from editing
     (their format is fixed).

At most `MAX_SWAPS` swaps are accepted, and an incoming sentence goes through the same checks
(its reference sentence is the DB `text`). A swap whose outgoing and incoming bullets are in
different sections is rejected — the per-section count stays exactly as in the base résumé.
"""

import json
import os
import re

import yaml

import config
import keyword_extractor
import tailor_engine

BASE_YAML_PATH = os.path.join(config.PROJECT_ROOT, "base_resume.yaml")

MAX_SWAPS = 2
# **At least one swap** is the default (your instruction: "swap one or two bullets on top of
# the latest version"). One second-pass run ended with zero swaps and produced a draft
# effectively identical to the base résumé — given the slack of "zero if there is no reason to
# change anything", the model picks zero. If no swap survives validation, the rejection
# reasons are attached as feedback and the plan is requested **one more time** (if it is still
# zero the base résumé ships as is and the report records that — the code never manufactures a
# forced swap).
MIN_SWAPS = 1
# An edit (reworking the sentence of the same experience) has to keep nearly all of the
# original words — below this it is a rewrite.
MIN_WORD_OVERLAP = 0.7
# A swap writes up a **different experience** in the style of the base résumé, so more words
# change relative to the DB original. At a threshold of 0.7 every legitimate swap was rejected
# (measured once: 63%, 8%).
MIN_WORD_OVERLAP_SWAP = 0.45
# An edit **cannot get longer** (your rules: be concise, and stop a pointless word or two from
# adding a line). Only same-length rewording or getting shorter is allowed.
MAX_LEN_GROWTH = 0
# A swap brings in a different experience, so it is allowed up to the longest bullet in that
# section (beyond that the line count grows).
SWAP_LEN_GROWTH = 0
# **Absolute base-résumé style caps** applied to a swap sentence — measured across the 12 base
# bullets (at most 3 commas, 1 semicolon, 1 set of parentheses). A swap cannot use "no growth
# over the original", because its reference sentence is a short one-liner from the DB.
SWAP_MAX_COMMAS = 3
SWAP_MAX_SEMICOLONS = 1
SWAP_MAX_PARENS = 1
# A total slightly over the base is allowed (about a third of a line) and the final verdict is
# a measurement in Word — with zero slack even a single 2-character edit got reverted and the
# JD tailoring disappeared altogether (measured once).
TOTAL_GROWTH_SLACK = 40
# Lines whose format is fixed — excluded from edits and swaps (same reason as
# pagefit.PROTECTED_PREFIXES).
PROTECTED_PREFIXES = ("Skills:", "Languages:")
_SHORTHAND_PATTERNS = (
    (r"\s[x×]\s", "formula-style chaining (x)"),
    (r"\bvs\.?\b", "vs comparison"),
    (r"~\s*\d", "tilde approximation"),
    (r"\d\s*/\s*\d", "slash-separated numbers"),
)


def load_base(path=BASE_YAML_PATH):
    """Read the base bullet set.

    Returns: {"source_docx", "sections": [{name, header_text, bullets}]}"""
    with open(path, "r", encoding="utf-8") as f:
        base = yaml.safe_load(f)
    seen = set()
    for section in base["sections"]:
        for bullet in section["bullets"]:
            if not bullet.get("id") or not bullet.get("text"):
                raise ValueError(f"base_resume.yaml entry has no id/text: {bullet!r}")
            if bullet["id"] in seen:
                raise ValueError(f"duplicate id in base_resume.yaml: {bullet['id']}")
            seen.add(bullet["id"])
    return base


def base_index(base):
    """{id: {"text", "section", "merged_ids"}} — used by edit validation to find the original
    sentence and the source of each figure."""
    return {b["id"]: {"text": b["text"], "section": s["name"],
                      "merged_ids": b.get("merged_ids") or []}
            for s in base["sections"] for b in s["bullets"]}


def _section_bullets(base, section_name):
    return next(s["bullets"] for s in base["sections"] if s["name"] == section_name)


def base_total_chars(base):
    return sum(len(b["text"]) for s in base["sections"] for b in s["bullets"])


def is_protected(text):
    return text.strip().startswith(PROTECTED_PREFIXES)


def _words(text):
    return [w for w in re.split(r"[^A-Za-z0-9%$+.]+", text.lower()) if w]


def _word_overlap(original, edited):
    """How much of the original wording the edit kept (0-1). Measured against the original, so
    dropping words also lowers the score."""
    orig = _words(original)
    if not orig:
        return 1.0
    edited_pool = list(_words(edited))
    kept = 0
    for w in orig:
        if w in edited_pool:
            edited_pool.remove(w)
            kept += 1
    return kept / len(orig)


def _numbers(text):
    """The set of numeric tokens in a sentence (normalised so notation differences compare)."""
    raw = re.findall(r"\d[\d,.]*\s*[%KkMmBb+]?", text or "")
    out = set()
    for token in raw:
        norm = token.replace(",", "").replace(" ", "").rstrip(".").lower()
        if norm:
            out.add(norm)
    return out


def _allowed_numbers(original_text, db_bullet):
    allowed = _numbers(original_text)
    for bullet in (db_bullet if isinstance(db_bullet, (list, tuple)) else [db_bullet]):
        if not bullet:
            continue
        allowed |= _numbers(bullet.get("text", ""))
        for value in (bullet.get("context") or {}).values():
            allowed |= _numbers(str(value))
    return allowed


def _db_index(db_sections):
    return {b["id"]: b for s in db_sections for b in s["bullets"]}


def check_text(new_text, original_text, db_bullet, label="", min_overlap=MIN_WORD_OVERLAP,
               max_len=None, swap=False, max_words=None):
    """Check whether an edited/swapped sentence is acceptable. Returns: the rejection reason
    (None if there is none).

    min_overlap: lower bound on retention of the original words (edit 0.7 / swap 0.45).
    max_len: length cap. If None, `original_text` length + MAX_LEN_GROWTH.
    swap: is this a swap sentence? A swap writes a **different experience** up in the style of
      the base résumé, so its reference sentence is a short one-liner from the DB. Applying the
      no-growth rules ("cannot be longer than the original" for word count, commas, parentheses
      and semicolons) and "do not lose a single figure from the original" as they stand rejects
      almost every legitimate swap (measured once: four proposed swaps fell one after another on
      "word count went up" and "quantified result lost: ['40']", ending with zero swaps).
      A swap instead gets **absolute base-résumé style caps** (SWAP_MAX_* and max_words below),
      and is required to keep **at least one** figure.
    max_words: word-count cap for a swap sentence (the longest base bullet in that section)."""
    if not new_text or not new_text.strip():
        return "empty sentence"
    if "—" in new_text or "–" in new_text:
        return "em dash/en dash used"
    limit = max_len if max_len is not None else len(original_text) + MAX_LEN_GROWTH
    if len(new_text) > limit:
        return f"too long ({len(new_text)} chars > cap {limit} chars)"
    overlap = _word_overlap(original_text, new_text)
    if overlap < min_overlap:
        return f"word retention {overlap:.0%} < {min_overlap:.0%} (a rewrite, not an edit)"
    for pattern, name in _SHORTHAND_PATTERNS:
        if re.search(pattern, new_text) and not re.search(pattern, original_text):
            return f"telegraphic notation added ({name})"
    if swap:
        # Caps measured on the base bullets (at most 3 commas, 1 semicolon, 1 set of
        # parentheses, 49 words).
        if new_text.count("(") > SWAP_MAX_PARENS:
            return f"{new_text.count('(')} parentheses > {SWAP_MAX_PARENS} (base style cap)"
        if new_text.count(";") > SWAP_MAX_SEMICOLONS:
            return f"{new_text.count(';')} semicolons > {SWAP_MAX_SEMICOLONS} (base style cap)"
        if new_text.count(",") > SWAP_MAX_COMMAS:
            return f"{new_text.count(',')} commas > {SWAP_MAX_COMMAS} (base style cap)"
        if max_words and len(_words(new_text)) > max_words:
            return f"{len(_words(new_text))} words > {max_words} (section base cap)"
    else:
        if new_text.count("(") > original_text.count("("):
            return "parentheses added (no bolting on extra detail)"
        if new_text.count(";") > original_text.count(";"):
            return "semicolon added (sentence complexity up)"
        if new_text.count(",") > original_text.count(",") + 1:
            return "commas added, so the clauses multiplied (concision violated)"
        if len(_words(new_text)) > len(_words(original_text)):
            return "word count went up (concision violated)"
    # The instruction to describe things at the higher level — pulling in a proper noun or tool
    # name the original did not have adds detail. The first word of a sentence is the lead verb,
    # so it is excluded.
    new_proper = _proper_nouns(new_text) - _proper_nouns(original_text)
    if new_proper:
        return f"proper noun not in the original added (detail creep): {sorted(new_proper)}"
    new_numbers = _numbers(new_text)
    allowed = _allowed_numbers(original_text, db_bullet)
    invented = {n for n in new_numbers if n not in allowed}
    if invented:
        return f"figure not in the original or the DB invented: {sorted(invented)}"
    # Quantified results come first: if even one figure is lost, reject. The older standard
    # ("reject only when they all disappear") let two of three leak away.
    # A swap is the exception — it fits a different experience into less space, so secondary
    # figures may drop. But **a swap that loses the quantified result entirely is rejected**
    # (the figure is the reason that bullet exists).
    if swap:
        if _numbers(original_text) and not new_numbers:
            return "no quantified result (the swap sentence has no figure at all)"
    elif len(new_numbers) < len(_numbers(original_text)):
        lost = sorted(_numbers(original_text) - new_numbers)
        return f"quantified result lost: {lost}"
    return None


def _proper_nouns(text):
    """Tokens that look like a proper noun or product name in the middle of a sentence.

    The first word (the lead verb) is excluded, and acronyms and their plurals do not count as
    proper nouns — `SMBs`, `KPIs` and `AI` are legitimate word substitutions that match the
    JD's wording. What has to be caught are names like `Airflow` or `QuickBooks` that drag in
    implementation detail or a product (an uppercase letter followed by two or more
    lowercase)."""
    words = re.findall(r"[A-Za-z][\w./-]*", text)
    out = set()
    for word in words[1:]:
        if not word[0].isupper():
            continue
        if sum(1 for ch in word[1:] if ch.islower()) >= 2:
            out.add(word)
    return out


PLAN_PROMPT_TEMPLATE = """\
You are a career coach making the **smallest possible** adjustments to an MBA internship
applicant's **finished base résumé** so that it fits one specific job description (JD). This is
not a job of writing new sentences — the base sentences are already the result of the
applicant's own hand-editing, and they are the quality bar.

[Base résumé bullets] (section / id / sentence)
{base_blob}

[JD ideal-candidate analysis]
{jd_profile_json}

[Repeated JD keywords] (wording that genuinely appears several times in the JD text,
descending by frequency — for ATS matching)
{keyword_blob}

[The company's own wording / philosophy] (what this company calls its work, its customers and
its products, and the values it stresses)
{company_voice_blob}

[Swap candidates] (other experiences from the experience DB that may be relevant to this JD,
by section)
{candidates_blob}

[Bullets that must not be dropped] (the user requires these on this application — excluded
from swapping)
{keep_blob}

Do only the following two things.

1. **Word/phrase substitution (edits)**: in base bullets that touch on the JD, switch to the
   JD's or the company's wording **only where they have a different word for the same thing**.
   For example, where the base says "customers" and the JD repeatedly says "small businesses",
   use that wording. Prefer vocabulary from [Repeated JD keywords] first and [The company's own
   wording / philosophy] second, in that order.
   - **Keep the meaning exactly as it is** — only the words change. Do not make a substitution
     that changes a fact, a figure or a causal relationship, or that widens or narrows the
     scope (for example turning "sourced" into "led", or turning "estimated" into a flat
     assertion). If the sentence is not actually about that, do not substitute — twisting a
     fact to match vocabulary is the worst outcome.
   - Do not put a bullet in edits if there is no reason to touch it (zero is fine; forcing a
     change is the worst thing you can do).
2. **Bullet swaps (swaps)**: replace **{min_swaps}-{max_swaps}** of the base bullets that are
   furthest from this JD with closer experiences from [Swap candidates] — swapping is the core
   of this task, so **propose at least {min_swaps}**. Swap only within the same section. Picking
   the single base bullet that is furthest from this JD is always possible (they are never all
   equally relevant).
   You write the incoming sentence yourself, and it must follow the [Sentence rules] below and
   use only facts present in the DB's text/context.
   - **A swap that repeats the same event as a bullet that stays on the résumé is forbidden** —
     another angle on the same project, or reusing the same figures, reads as one experience
     stretched across two lines (the code rejects it for duplicate figures). Only swap in a way
     that widens the range of experience.

[Priority] (your instructions — when they conflict, the higher one wins)
1. **Showing a distinctive capability and a quantified result comes first.** The figures
   (scale, amount, multiple, rate) and the applicant's own judgement that produced them must
   stay in the sentence. An edit that drops even one figure is rejected by the code.
2. **A sentence a first-time reader understands in one pass.** Do not wedge another clause into
   a sentence — an edit that adds commas, semicolons or parentheses is rejected.
3. **Describe things at the higher level.** Do not drag in detail such as internal jargon, tool
   names or implementation specifics (a proper noun absent from the original gets rejected). Do
   not spell out in more detail somewhere the base résumé already wrote at the higher level.
4. **Do not let a pointless word or two add a line.** That is why an edit must be **the same
   length or shorter** — an edit that gets longer is rejected. If the meaning is the same, the
   shorter version is always the right one.

[Sentence rules] (apply to both edits and swaps — break one and the code rejects it and keeps
the base sentence)
- Structure: **verb + What (what you did) + Why (why you did it / what the problem was) + How
  (how you solved it) + Impact (the result)**.
  Every base sentence has this structure. If it does not read clearly in one pass, it failed.
- Length: an edit **cannot get longer** than the base sentence (same or shorter). A swap
  sentence cannot exceed the longest base bullet in that section.
- Keep at least {min_overlap:.0%} of the base wording as it is (this is an edit, not a
  rewrite). Do not increase the word count either.
- Use only figures that are in the base sentence or in the DB. Do not invent a new figure or a
  new fact.
- Forbidden notation: em dash (—), formula-style chaining such as `A x B x C`, `vs`, tilde
  approximations such as `~20%`, and added parentheses or semicolons. This kind of compressed
  notation was the actual cause of "I cannot tell what this sentence says".
- Lines starting with `Skills:` / `Languages:` have a fixed format — never touch them.

{feedback_blob}Output only the JSON below (no commentary, no code fences):
{{
  "edits": [
    {{"id": "<base bullet id>", "text": "<edited sentence>",
      "why": "<one line on which JD wording/requirement this matches>"}}
  ],
  "swaps": [
    {{"out": "<base bullet id to drop>", "in": "<DB bullet id to bring in>",
      "text": "<sentence to insert>",
      "why": "<one line on why this swap fits this JD better>"}}
  ],
  "keyword_notes": ["<a JD keyword you had to give up because it could not go on the
                     résumé naturally + why>"]
}}
"""


def _base_blob(base):
    lines = []
    for section in base["sections"]:
        lines.append(f"## {section['name']}")
        for bullet in section["bullets"]:
            lines.append(f"- [{bullet['id']}] ({len(bullet['text'])} chars) {bullet['text']}")
    return "\n".join(lines)


def _candidates_blob(base, db_sections, limit_per_section=8):
    """Show only DB bullets that are not already in the base résumé as swap candidates (so
    nothing already on the page is recommended again).

    Bullets with `retired: true` are **left out of the list entirely** so the LLM cannot even
    see them — in one run a retired bullet (the predecessor of a discontinued side project) was
    swapped in. At the time the exclusion signal existed only in the rebuild path's prompt
    (resume_db.to_prompt_blob) and in tailor_engine's post-filter, so the minimal path had no
    defence at all. Deleting them from the list is more reliable than writing "do not pick
    these" in the prompt."""
    in_base = set(base_index(base))
    lines = []
    for section in db_sections:
        rows = [b for b in section["bullets"]
                if b["id"] not in in_base and not is_protected(b["text"])
                and not b.get("retired")]
        if not rows:
            continue
        lines.append(f"## {section['name']}")
        for bullet in rows[:limit_per_section]:
            tags = bullet.get("tags") or {}
            keywords = ", ".join((tags.get("keywords") or [])[:6])
            lines.append(f"- [{bullet['id']}] {bullet['text']}")
            if keywords:
                lines.append(f"    keywords: {keywords}")
    return "\n".join(lines) or "(no swap candidates)"


DUP_NUMBER_OVERLAP = 2


def _duplicate_of_kept(text, kept_texts):
    """Decide whether an incoming swap sentence is **the same event as another bullet that stays
    on the résumé**.

    The test is figure overlap — sharing {n} or more figures means the same work was written up
    on two lines (measured once: as soon as one swap was guaranteed, `example-claim2-backend`
    came in and repeated `example-claim2`'s "4,000 tickets / 18%" verbatim, and the recruiter
    review raised "the same launch stretched over two lines, which makes a reader doubt the
    breadth of the experience" as a top finding). The outgoing bullet is not part of the
    comparison, so a swap that **moves across** to a different angle on the same experience is
    still allowed.
    Returns: the list of figures shared with the sentence judged duplicate (None if there is
    none).
    """.replace("{n}", str(DUP_NUMBER_OVERLAP))
    incoming = set(_numbers(text))
    if not incoming:
        return None
    for kept in kept_texts:
        shared = incoming & set(_numbers(kept))
        if len(shared) >= DUP_NUMBER_OVERLAP:
            return sorted(shared)
    return None


def _company_voice_blob(jd_profile):
    """The vocabulary the company uses and the values it stresses — the second dictionary for
    word substitution (the first is the repeated JD keywords).

    Your instruction: "use the words that fit the JD or the company's philosophy, but keep the
    meaning". This only reuses output that has already been produced for free:
    `jd_extractor.extract_jd_profile`'s culture_values/company_vocabulary (part of the single
    call it already makes) and the company research summary. It adds no extra LLM call.
    """
    rows = []
    for key, label in (("company_vocabulary", "Wording the company uses"),
                       ("culture_values", "Values/philosophy it stresses")):
        values = jd_profile.get(key) or []
        if isinstance(values, str):
            values = [values]
        values = [str(v).strip() for v in values if str(v).strip()]
        if values:
            rows.append(f"- {label}: " + ", ".join(values[:10]))
    notes = ((jd_profile.get("company_research") or {}).get("notes") or "").strip()
    if notes:
        rows.append(f"- Recent developments (research): {notes[:300]}")
    if not rows:
        return ("(no company-specific wording found in the JD — rely on "
                "[Repeated JD keywords] alone)")
    return "\n".join(rows)


def _feedback_blob(applied):
    """The block attached to the second request when the first plan came back with zero swaps —
    it hands the rejection reasons straight back."""
    lines = ["[First plan rejected]",
             f"This plan was not accepted because it had fewer than {MIN_SWAPS} swap(s). "
             f"Pick the base bullet that is furthest from this JD and propose a swap for it."]
    rejected = (applied or {}).get("rejected") or []
    if rejected:
        lines.append("Items the code rejected on the first pass (do not repeat the same mistake):")
        for row in rejected[:6]:
            lines.append(f"- {row.get('id')}: {row.get('why')}")
    return "\n".join(lines) + "\n\n"


def plan(backend, base, db_sections, jd_profile, keyword_counts=None, keep_ids=(),
         feedback="", log=print):
    """Get an edits/swaps plan from the LLM (apply_plan does the validation). One call.

    feedback: the block attached to the second request when the first plan was rejected
    (`_feedback_blob`)."""
    prompt = PLAN_PROMPT_TEMPLATE.format(
        base_blob=_base_blob(base),
        jd_profile_json=json.dumps(jd_profile, ensure_ascii=False, indent=2),
        keyword_blob=keyword_extractor.format_for_prompt(keyword_counts or []),
        company_voice_blob=_company_voice_blob(jd_profile),
        candidates_blob=_candidates_blob(base, db_sections),
        min_swaps=MIN_SWAPS,
        max_swaps=MAX_SWAPS,
        min_overlap=MIN_WORD_OVERLAP,
        keep_blob=(", ".join(keep_ids) if keep_ids else "(none)"),
        feedback_blob=feedback or "",
    )
    log(f"  requesting the minimal-edit plan ({len(prompt) // 1024}KB)...")
    raw = backend.complete(prompt)
    data = tailor_engine._parse_json_response(raw)
    return {"edits": data.get("edits") or [], "swaps": data.get("swaps") or [],
            "keyword_notes": data.get("keyword_notes") or []}


def apply_plan(base, db_sections, plan_data, keep_ids=(), log=print):
    """Validate the plan and apply it. Returns: (sections_result, applied)

    sections_result: the {"sections": {name: [{id,text}]}} shape resume_writer.render_sections
      expects.
    applied: the record kept for the report/audit
      {"edits": [...], "swaps": [...], "rejected": [...]}
    """
    db = _db_index(db_sections)
    idx = base_index(base)
    edits = {}
    swaps = {}
    applied = {"edits": [], "swaps": [], "rejected": [], "keyword_notes": plan_data.get("keyword_notes") or []}

    planned_outs = {item.get("out") for item in plan_data.get("swaps", [])[:MAX_SWAPS]}
    for item in plan_data.get("swaps", [])[:MAX_SWAPS]:
        out_id, in_id, text = item.get("out"), item.get("in"), item.get("text")
        if out_id not in idx:
            applied["rejected"].append(
                {"id": out_id, "why": "tried to drop a bullet that is not in the base"})
            continue
        if in_id not in db:
            applied["rejected"].append(
                {"id": in_id, "why": "tried to bring in a bullet that is not in the DB"})
            continue
        # They are already left out of the candidate list (_candidates_blob), but the LLM can
        # still send an id it saw in the base résumé or another context, so block it once more
        # at the apply stage.
        if db[in_id].get("retired"):
            applied["rejected"].append(
                {"id": in_id, "why": "tried to bring in a retired experience"})
            continue
        in_section = next(s["name"] for s in db_sections if any(b["id"] == in_id for b in s["bullets"]))
        if in_section != idx[out_id]["section"]:
            applied["rejected"].append(
                {"id": in_id,
                 "why": f"section mismatch ({in_section} → {idx[out_id]['section']})"})
            continue
        if is_protected(idx[out_id]["text"]):
            applied["rejected"].append(
                {"id": out_id, "why": "tried to swap a fixed-format line (Skills/Languages)"})
            continue
        if out_id in set(keep_ids):
            applied["rejected"].append({"id": out_id, "why": "bullet the user pinned with --keep"})
            continue
        # A swap brings in a **different experience**, so the reference for the sentence checks
        # (word retention, figures, structure) is **the DB original of that bullet**, not the
        # base sentence going out. The outgoing sentence is used only as the length budget (so
        # the volume does not exceed the base) — in the first run both were treated as one
        # reference and a legitimate swap was rejected at "word retention 8%".
        # The length cap is **the longest base bullet in that section**. Anchored on the
        # outgoing sentence instead, every swap that drops a short bullet is rejected (measured
        # once: 209 chars rejected in the slot of a 180-char bullet).
        kept_texts = [info["text"] for bid, info in idx.items()
                      if bid != out_id and bid not in planned_outs]
        kept_texts += [sw["text"] for sw in swaps.values()]
        duplicate = _duplicate_of_kept(text, kept_texts)
        if duplicate:
            applied["rejected"].append(
                {"id": in_id,
                 "why": f"swap rejected: same experience as a bullet that stays "
                        f"(figures {duplicate} repeated)"})
            continue
        section_rows = [b for b in _section_bullets(base, idx[out_id]["section"])
                        if not is_protected(b["text"])]
        section_max = max(len(b["text"]) for b in section_rows)
        section_max_words = max(len(_words(b["text"])) for b in section_rows)
        reason = check_text(text, db[in_id]["text"], db[in_id], label=in_id,
                            min_overlap=MIN_WORD_OVERLAP_SWAP,
                            max_len=section_max + SWAP_LEN_GROWTH,
                            swap=True, max_words=section_max_words)
        if reason:
            applied["rejected"].append({"id": in_id, "why": f"swap rejected: {reason}"})
            continue
        swaps[out_id] = {"id": in_id, "text": text, "why": item.get("why", "")}
        applied["swaps"].append({"out": out_id, "in": in_id, "why": item.get("why", ""),
                                 "text": text})

    for item in plan_data.get("edits", []):
        bullet_id, text = item.get("id"), item.get("text")
        if bullet_id not in idx:
            applied["rejected"].append(
                {"id": bullet_id, "why": "tried to edit a bullet that is not in the base"})
            continue
        if bullet_id in swaps:
            continue  # editing a bullet that is being swapped out is pointless
        original = idx[bullet_id]["text"]
        if is_protected(original):
            applied["rejected"].append(
                {"id": bullet_id,
                 "why": "tried to edit a fixed-format line (Skills/Languages)"})
            continue
        if text.strip() == original.strip():
            continue
        fact_sources = [db.get(bullet_id)] + [db.get(i) for i in idx[bullet_id]["merged_ids"]]
        reason = check_text(text, original, fact_sources, label=bullet_id)
        if reason:
            applied["rejected"].append({"id": bullet_id, "why": f"edit rejected: {reason}"})
            continue
        edits[bullet_id] = text
        applied["edits"].append({"id": bullet_id, "before": original, "after": text,
                                 "why": item.get("why", "")})

    sections = {}
    for section in base["sections"]:
        rows = []
        for bullet in section["bullets"]:
            if bullet["id"] in swaps:
                rows.append({"id": swaps[bullet["id"]]["id"], "text": swaps[bullet["id"]]["text"]})
            else:
                rows.append({"id": bullet["id"], "text": edits.get(bullet["id"], bullet["text"])})
        sections[section["name"]] = rows

    # If the total exceeds the base, **revert the edits that grew the most, first** — the base
    # is one page, so keeping the total at or below the base makes length accidents structurally
    # impossible (this is what stops the two-page accident from recurring).
    budget = base_total_chars(base) + TOTAL_GROWTH_SLACK
    def _total():
        return sum(len(r["text"]) for rows in sections.values() for r in rows)
    if _total() > budget:
        growth = sorted(applied["edits"], key=lambda e: len(e["after"]) - len(e["before"]), reverse=True)
        for entry in growth:
            if _total() <= budget:
                break
            if len(entry["after"]) <= len(entry["before"]):
                continue
            for rows in sections.values():
                for row in rows:
                    if row["id"] == entry["id"]:
                        row["text"] = entry["before"]
            applied["rejected"].append(
                {"id": entry["id"], "why": "edit reverted: total budget exceeded"})
            applied["edits"] = [e for e in applied["edits"] if e["id"] != entry["id"]]
            log(f"  [reverted] {entry['id']}: total budget exceeded")

        # If it still overflows after reverting every edit, revert the swaps too (restoring the
        # base bullet) — no path is left that ships with more volume than the base.
        for entry in sorted(applied["swaps"], key=lambda e: len(e["text"]), reverse=True):
            if _total() <= budget:
                break
            base_text = idx[entry["out"]]["text"]
            for rows in sections.values():
                for row in rows:
                    if row["id"] == entry["in"]:
                        row["id"], row["text"] = entry["out"], base_text
            applied["rejected"].append(
                {"id": entry["in"], "why": "swap reverted: total budget exceeded"})
            applied["swaps"] = [sw for sw in applied["swaps"] if sw["in"] != entry["in"]]
            log(f"  [reverted] swap {entry['out']} → {entry['in']}: total budget exceeded")

    for entry in applied["swaps"]:
        log(f"  [swap] {entry['out']} → {entry['in']} ({entry['why'][:60]})")
    for entry in applied["edits"]:
        log(f"  [edit] {entry['id']}: {len(entry['before'])} chars → {len(entry['after'])} chars "
            f"({entry['why'][:60]})")
        log(f"      before: {entry['before']}")
        log(f"      after:  {entry['after']}")
    for entry in applied["rejected"]:
        log(f"  [rejected] {entry['id']}: {entry['why']}")
    if not applied["edits"] and not applied["swaps"]:
        log("  No change — the base ships as is (there is no reason to touch it "
            "for this JD).")

    return {"sections": sections, "gaps": [], "cuts": {}, "close_calls": [],
            "minimal_edit": applied}, applied


def run(backend, base, db_sections, jd_profile, keyword_counts=None, keep_ids=(), log=print):
    """plan → apply. On failure it returns **the base as is** (fail-soft, so the draft is never
    blocked).

    keep_ids: ids that must go on this application (`tailor --keep`) — they are never swapped
    out.

    If fewer than `MIN_SWAPS` swaps survive, the rejection reasons are attached and the plan is
    requested **one more time** (your instruction: swapping one or two bullets on top of the base
    is the core of this task). If the retry is also zero, the first result is used as it is —
    the code never manufactures a forced swap."""
    def _ask(feedback=""):
        try:
            return plan(backend, base, db_sections, jd_profile, keyword_counts=keyword_counts,
                        keep_ids=keep_ids, feedback=feedback, log=log)
        except Exception as e:
            log(f"  [minimal-edit plan failed — using the base as is] {e}")
            return {"edits": [], "swaps": [], "keyword_notes": []}

    result, applied = apply_plan(base, db_sections, _ask(), keep_ids=keep_ids, log=log)
    if len(applied["swaps"]) >= MIN_SWAPS:
        return result, applied

    log(f"  [re-request] only {len(applied['swaps'])} valid swap(s) — asking for the plan again "
        f"with the rejection reasons attached (one extra LLM call)")
    retry_result, retry_applied = apply_plan(base, db_sections, _ask(_feedback_blob(applied)),
                                             keep_ids=keep_ids, log=log)
    if len(retry_applied["swaps"]) >= MIN_SWAPS:
        return retry_result, retry_applied
    log("  [no swap] two requests produced no swap that passed — keeping the base bullet set.")
    # Keep the first result (it may have more edits; the second pass trades edits away trying to
    # produce a swap).
    applied["no_swap"] = True
    return result, applied


def revert_largest(result, base, log=print):
    """When the volume overflows, **revert the single edit/swap that grew the most** (restoring
    the base sentence).

    This is the one-page guarantee of minimal mode — it does not compress sentences (compression
    is exactly the step that produced "sentences you cannot understand"). The base itself is one
    page, so repeating the revert always reaches one page.
    Returns: a description of what was reverted (None if there is nothing left to revert)."""
    applied = result.get("minimal_edit") or {}
    idx = base_index(base)
    candidates = []
    for entry in applied.get("edits", []):
        candidates.append((len(entry["after"]) - len(entry["before"]), "edit", entry))
    for entry in applied.get("swaps", []):
        candidates.append((len(entry["text"]) - len(idx[entry["out"]]["text"]), "swap", entry))
    if not candidates:
        return None
    # **Revert edits first.** An edit is a wording substitution (low value), whereas a swap put
    # an experience that fits this JD better on the page (high value) — discarding the
    # high-value change first for volume reasons removes the tailoring itself.
    edits_first = [c for c in candidates if c[1] == "edit"] or candidates
    _growth, kind, entry = max(edits_first, key=lambda c: c[0])

    if kind == "edit":
        target_id, new_id, new_text = entry["id"], entry["id"], entry["before"]
        applied["edits"] = [e for e in applied["edits"] if e["id"] != entry["id"]]
        label = f"edit {entry['id']}"
    else:
        target_id, new_id = entry["in"], entry["out"]
        new_text = idx[entry["out"]]["text"]
        applied["swaps"] = [s for s in applied["swaps"] if s["in"] != entry["in"]]
        label = f"swap {entry['out']} → {entry['in']}"

    for rows in result["sections"].values():
        for row in rows:
            if row["id"] == target_id:
                row["id"], row["text"] = new_id, new_text
    applied.setdefault("rejected", []).append(
        {"id": target_id, "why": "reverted: ran over one page"})
    log(f"  [reverted] {label}: ran over one page, restored the base sentence")
    return label


# ── Removing wasted lines (widows) — deletion-only editing ───────────────────────
# Your first instruction: "stop a pointless word or two in a bullet from adding a line".
# The only thing the LLM is allowed to do here is **delete words** (no rewriting). That is what
# makes the result checkable in code — every word of the new sentence must appear in the
# original, in the same order. The old pagefit compression allowed free rewriting, which is how
# sentences like "held to ~20%/~5% vs 60-70% claims" were produced.
WIDOW_MAX_WORDS = 2

SHORTEN_PROMPT_TEMPLATE = """\
The résumé bullet below leaves only a word or two on its last line, wasting a whole line.
**Cut at least {need} characters.**

Rules (break one and the code rejects the result and keeps the original):
- **Deleting words is the only thing allowed.** Do not write a new word and do not reword
  anything — every word that remains must be a word from the original, in the original order
  (dropping function words and articles is fine, substituting a word is not).
- Do not drop a single figure (scale, amount, rate, multiple). The quantified result is the
  point of this sentence.
- The sentence structure (verb + What + Why + How + Impact) and the flow of the reading must
  survive.
- What you delete are modifiers, duplicated explanation and context words the reader can take
  for granted.

Original ({length} chars):
{text}

Output only the shortened sentence, on one line (no commentary, quotes or code fences).
"""


def is_deletion_only(original, shortened):
    """Check that `shortened` is **only the original with words deleted** (order preserved, no
    new words)."""
    orig_words = _words(original)
    new_words = _words(shortened)
    i = 0
    for word in new_words:
        while i < len(orig_words) and orig_words[i] != word:
            i += 1
        if i == len(orig_words):
            return False
        i += 1
    return True


def shorten_deletion_only(backend, text, need_chars, db_bullet=None, log=print):
    """Cut at least need_chars to remove a widow line. Returns: the shortened sentence (None on
    failure)."""
    prompt = SHORTEN_PROMPT_TEMPLATE.format(need=need_chars, length=len(text), text=text)
    try:
        raw = backend.complete(prompt)
    except Exception as e:
        log(f"  [shorten failed (skipped)] {e}")
        return None
    candidate = (raw or "").strip().strip('"').splitlines()[0].strip() if raw else ""
    if not candidate:
        return None
    if len(text) - len(candidate) < need_chars:
        log(f"  [shorten rejected] {need_chars} chars required but only "
            f"{len(text) - len(candidate)} cut")
        return None
    if not is_deletion_only(text, candidate):
        log("  [shorten rejected] not word deletion but a rewrite (new words/changed order)")
        return None
    reason = check_text(candidate, text, db_bullet, min_overlap=0.0, max_len=len(text))
    if reason:
        log(f"  [shorten rejected] {reason}")
        return None
    return candidate


def remove_widows(path, backend, base, db_sections, log=print, max_rounds=2):
    """Remove the docx's widow bullets (last line of WIDOW_MAX_WORDS words or fewer) with
    deletion-only compression.

    Requires a measurement in Word (skipped silently if unavailable). Returns: the number of
    bullets fixed.
    """
    import pagefit  # deferred import to avoid a circular import

    if not pagefit.word_available() or backend is None:
        return 0
    db = _db_index(db_sections)
    idx = base_index(base)
    fixed = 0
    for _round in range(max_rounds):
        try:
            measurement = pagefit.measure(path)
        except pagefit.WordUnavailable as e:
            log(f"  [widow check skipped] {e}")
            return fixed
        widows = [b for b in measurement["bullets"]
                  if b["last_line_words"] <= WIDOW_MAX_WORDS and b["lines"] > 1
                  and not is_protected(b["text"])]
        if not widows:
            if _round == 0:
                log("  No wasted lines (widows)")
            return fixed
        progressed = False
        for bullet in widows:
            need = bullet["last_line_chars"] + 3
            # Find which base bullet that sentence is, and attach its figure sources (the DB).
            bullet_id = next((i for i, meta in idx.items() if meta["text"] == bullet["text"]), None)
            sources = ([db.get(bullet_id)] + [db.get(m) for m in idx[bullet_id]["merged_ids"]]
                       if bullet_id else None)
            log(f"  [widow] paragraph {bullet['index']}: last line has "
                f"{bullet['last_line_words']} word(s) (\"{bullet['last_line']}\") → "
                f"trying to delete at least {need} chars")
            shortened = shorten_deletion_only(backend, bullet["text"], need,
                                              db_bullet=sources, log=log)
            if not shortened:
                continue
            pagefit.set_bullet_text(path, bullet["index"], shortened)
            log(f"      {len(bullet['text'])} chars → {len(shortened)} chars: {shortened}")
            fixed += 1
            progressed = True
        if not progressed:
            return fixed
    return fixed
