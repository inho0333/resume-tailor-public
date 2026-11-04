import yaml

REQUIRED_SECTION_FIELDS = {"name", "header_text", "bullets"}
REQUIRED_BULLET_FIELDS = {"id", "text", "tags"}


def load_db(path):
    """Reads resume_db.yaml, validates it, and returns the list of sections.

    Validation: required fields on sections/bullets, and global uniqueness of bullet ids.
    On failure, raises ValueError with a message naming the offending section/bullet.
    """
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    sections = (data or {}).get("sections")
    if not sections:
        raise ValueError(f"The DB has no sections: {path}")

    seen_ids = set()
    for section in sections:
        missing = REQUIRED_SECTION_FIELDS - section.keys()
        if missing:
            raise ValueError(f"Section {section.get('name', '?')!r} is missing fields: {missing}")
        for bullet in section["bullets"]:
            missing = REQUIRED_BULLET_FIELDS - bullet.keys()
            if missing:
                raise ValueError(
                    f"Bullet {bullet.get('id', '?')!r} in section {section['name']!r} "
                    f"is missing required fields: {missing}"
                )
            if bullet["id"] in seen_ids:
                raise ValueError(f"Duplicate bullet id: {bullet['id']!r}")
            seen_ids.add(bullet["id"])

    return sections


def section_names(sections):
    return [s["name"] for s in sections]


def id_to_section_name(sections):
    """bullet id -> the name of the section it belongs to. Used to catch the LLM attaching an
    id to the wrong section."""
    return {b["id"]: s["name"] for s in sections for b in s["bullets"]}


def bullets_by_id(sections):
    return {b["id"]: b for s in sections for b in s["bullets"]}


def to_select_blob(sections):
    """Summary blob **for the selection stage** — ids, keywords and finished sentences only,
    without the context (why/how/impact).

    Why this was split out: the prompt that did "select + rewrite" in a single call had grown
    to 52.7KB, 39KB of which was this context (measured once: 41KB -> 52.7KB). But ACME CORP.
    has only 4 of its 14 candidates selected, so the background material for the 10 that get
    dropped was being sent every single time. What selection needs is "what the experience
    was"; the background material is only needed **when a selected bullet is actually
    rewritten**.

    Exclusion signals such as retired/superseded_by are used during selection, so they stay
    here."""
    lines = []
    for section in sections:
        lines.append(f"### {section['name']}")
        for b in section["bullets"]:
            tags = b.get("tags", {})
            keywords = ", ".join(tags.get("keywords", []))
            content_type = ", ".join(tags.get("content_type", []))
            lines.append(
                f"- id={b['id']}  [keywords: {keywords} | content_type: {content_type}]\n"
                f"  {b['text']}"
            )
            if b.get("retired"):
                lines.append("  ⛔ This experience is retired — never select it under any "
                             "circumstances. Even if you need to hit a count, use another bullet.")
            if b.get("superseded_by"):
                lines.append(f"  ⚠️ This is the predecessor of {b['superseded_by']}. "
                             f"If you select {b['superseded_by']}, exclude this bullet.")
    return "\n".join(lines)


def to_rewrite_blob(sections, selected_ids):
    """Blob **for the rewrite stage** — only the selected bullets, with all of their
    background material (why/how/impact) attached. Candidates dropped during selection are
    not included (see the to_select_blob comment)."""
    wanted = set(selected_ids)
    lines = []
    for section in sections:
        picked = [b for b in section["bullets"] if b["id"] in wanted]
        if not picked:
            continue
        lines.append(f"### {section['name']}")
        for b in picked:
            lines.append(f"- id={b['id']}")
            lines.append(f"  Verified core sentence (text): {b['text']}")
            if b.get("label"):
                lines.append(
                    f"  Category prefix (label): \"{b['label']}: \" — the rewritten sentence "
                    f"must start with this prefix (prompt rule 8). Use it verbatim."
                )
            context = b.get("context") or {}
            if context.get("why"):
                lines.append(f"  Background (why): {context['why']}")
            if context.get("how"):
                lines.append(f"  Approach (how): {context['how']}")
            if context.get("impact"):
                lines.append(f"  Outcome (impact): {context['impact']}")
    return "\n".join(lines)
