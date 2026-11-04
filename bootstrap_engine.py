"""LLM logic used only by the bootstrap command — it attaches keywords/content_type tags to
the raw bullets resume_parser pulled out, and flags bullets whose quantitative evidence is
thin by turning the weakness into a question.

Unlike tailor_engine.py, nothing here rewrites the bullet text itself (this keeps the "never
invent facts" rule in its most conservative form — a rewrite should only happen in the tailor
step, where the job description is known). The one exception is when the user answers a gap
question directly; then that fact alone is folded into the sentence
(refine_bullet_with_fact).
"""

import json
import re


def _parse_json_response(raw_text):
    cleaned = raw_text.strip()
    fence_match = re.search(r"```(?:json)?\s*(.*?)\s*```", cleaned, re.DOTALL)
    if fence_match:
        cleaned = fence_match.group(1)
    return json.loads(cleaned)


TAG_AND_GAP_PROMPT_TEMPLATE = """\
You are a career coach helping a candidate turn the raw text of their resume into a
structured experience database.

[Extracted sections and raw bullets] (every bullet carries a unique id)
{bullets_blob}

For each bullet, decide the following:
1. keywords: 3-6 keywords that describe this experience (e.g. investment, product management,
   data analysis — free vocabulary, only what actually matches the bullet's content).
2. content_type: 1-3 tags describing the nature of this bullet. Free vocabulary; examples:
   quantitative-impact, product-build, leadership-recognition, customer-research,
   investment-track-record, cross-functional-leadership, side-project, technical-skill,
   thought-leadership, mentorship. You may invent a new one if none of these fits.
3. gap: if this bullet is missing a quantitative outcome (numbers, %, scale) or an explanation
   of what/how/why, so that it would lack evidence when it is later rewritten for a different
   job posting, write one question that points precisely at what is missing
   (e.g. "How many people did the survey cover?", "Did this project improve any metric
   quantitatively?"). If it is already specific enough, leave gap as null.

Important: at this stage you only tag and identify what is missing. Never change or rewrite
the raw bullet text itself (rewriting happens in a later step, against the job posting).

Respond with this JSON schema only (JSON only, no explanatory prose):
{{
  "bullets": {{
    "<bullet id>": {{
      "keywords": ["...", "..."],
      "content_type": ["...", "..."],
      "gap": {{"question": "..."}} or null
    }},
    ...
  }}
}}
"""


def _format_bullets_blob(sections):
    """sections: [{"name", "bullets": [{"id", "text"}, ...]}, ...]"""
    lines = []
    for section in sections:
        lines.append(f"### {section['name']}")
        for b in section["bullets"]:
            lines.append(f"- [{b['id']}] {b['text']}")
    return "\n".join(lines)


def tag_and_find_gaps(backend, sections):
    """sections: [{"name", "header_text", "bullets": [{"id", "text"}, ...]}, ...]

    Returns: {bullet_id: {"keywords": [...], "content_type": [...],
    "gap": {"question": ...} | None}}
    If the LLM returns an id that does not exist, or breaks the format, that entry is skipped
    with a warning only (the same safety-net pattern tailor_engine uses).
    """
    valid_ids = {b["id"] for s in sections for b in s["bullets"]}
    prompt = TAG_AND_GAP_PROMPT_TEMPLATE.format(bullets_blob=_format_bullets_blob(sections))
    raw_text = backend.complete(prompt)
    result = _parse_json_response(raw_text)

    tags_by_id = {}
    for bid, info in (result.get("bullets") or {}).items():
        if bid not in valid_ids:
            print(f"  [warning] LLM returned a bullet id that does not exist, ignored: {bid!r}")
            continue
        info = info or {}
        gap = info.get("gap") or None
        if gap and not (gap.get("question") or "").strip():
            gap = None
        tags_by_id[bid] = {
            "keywords": [k for k in (info.get("keywords") or []) if k],
            "content_type": [c for c in (info.get("content_type") or []) if c],
            "gap": gap,
        }

    for bid in valid_ids - tags_by_id.keys():
        print(f"  [warning] bullet id missing from the LLM response, tags left empty: {bid!r}")
        tags_by_id[bid] = {"keywords": [], "content_type": [], "gap": None}

    return tags_by_id


REFINE_WITH_FACT_PROMPT_TEMPLATE = """\
Below is the raw text of a resume bullet, plus a fact the user supplied directly about what
that bullet was missing.

[Original]
{bullet_text}

[Question about what was missing]
{question}

[User's answer (trust it as fact and reflect it)]
{answer}

Rewrite the original as one sentence (or roughly the same length as the original), folding in
only the facts contained in that answer. Do not invent any additional outcome or figure that
is not in the answer. Keep what the original already said.

Respond with this JSON schema only (JSON only, no explanatory prose):
{{"text": "the rewritten bullet text"}}"""


def refine_bullet_with_fact(backend, bullet_text, question, answer):
    """Some backends force JSON mode inside backend.complete()
    (llm_client.OllamaBackend), so the request is wrapped in JSON instead of asking for plain
    prose — handled consistently with the same pattern tailor_engine uses
    (_parse_json_response)."""
    prompt = REFINE_WITH_FACT_PROMPT_TEMPLATE.format(
        bullet_text=bullet_text, question=question, answer=answer,
    )
    raw_text = backend.complete(prompt)
    result = _parse_json_response(raw_text)
    return (result.get("text") or bullet_text).strip()
