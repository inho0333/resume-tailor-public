"""Sentence-shape rules that apply to one family of roles, not to everyone.

Reads role_styles.yaml. See the comments in that file for why this is data: the
one entry it ships with was a hardcoded prompt rule attributed to a single
career centre, and it fired on every product-family posting regardless of whose
profile was loaded.

A missing or unreadable file means no rules, which is a working configuration —
the rewrite prompt's own rules (quantification, one-pass clarity, no em dash)
are the ones that apply to every role.
"""

import os

import yaml

PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "role_styles.yaml")

_cached = None


def load(path=PATH):
    """[{"name", "match": [str], "rules": [str]}]. [] when the file says nothing."""
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError) as e:
        print(f"  [warn] role_styles.yaml could not be read ({e}); no role-style rules")
        return []
    families = []
    for entry in (data.get("families") or []):
        if not isinstance(entry, dict):
            continue
        match = [str(m).strip().lower() for m in (entry.get("match") or []) if str(m).strip()]
        rules = [str(r).strip() for r in (entry.get("rules") or []) if str(r).strip()]
        if match and rules:
            families.append({"name": str(entry.get("name") or "").strip() or "(unnamed)",
                             "match": match, "rules": rules})
    return families


def get():
    global _cached
    if _cached is None:
        _cached = load()
    return _cached


def reset_cache():
    """For tests, which load families from fixtures."""
    global _cached
    _cached = None


def family_for(role, track=""):
    """The first family whose match list appears in the title, else in the track."""
    title = (role or "").lower()
    track = (track or "").lower()
    for family in get():
        if any(m in title for m in family["match"]):
            return family
    for family in get():
        if any(m in track for m in family["match"]):
            return family
    return None


def guidance_block(role, track=""):
    """The prompt block for this posting's role family. "" when none applies."""
    family = family_for(role, track)
    if not family:
        return ""
    lines = [f"[{family['name']} bullet style] (conventions for this family of roles — "
             f"see role_styles.yaml)"]
    for rule in family["rules"]:
        lines.append(f"   - {rule}")
    return "\n".join(lines)
