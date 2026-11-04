"""Settings.

Two kinds of value live here:

  • things about *you* — paths, section caps, which backend to use — which are
    read from user_config.yaml (gitignored; copy user_config.example.yaml). They
    have no personal default: a missing value is an error with instructions, not
    a silent fallback to someone else's file.
  • things about *the pipeline* — model choices, budgets, thresholds — which are
    constants here with a comment saying why that number.

Facts about who you are (degree, graduation, work authorisation, target roles)
are not here. They are in profile.yaml; see profile.py.
"""

import os

import yaml

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(PROJECT_ROOT, "output")

DB_PATH = os.path.join(PROJECT_ROOT, "resume_db.yaml")

# Your settings. `python run.py bootstrap --resume <your_resume.docx>` writes a
# first version of this file; after that you edit it by hand.
USER_CONFIG_PATH = os.environ.get("RESUME_TAILOR_USER_CONFIG") or os.path.join(
    PROJECT_ROOT, "user_config.yaml")


def _load_user_config():
    if not os.path.exists(USER_CONFIG_PATH):
        return {}
    with open(USER_CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


_user = _load_user_config()


def _path(value):
    return os.path.expanduser(value) if value else None


# ── your files ──────────────────────────────────────────────────────────────

# The .docx everything is rendered from: its fonts, margins and bullet
# formatting are what you get. None until user_config.yaml sets resume_path —
# run.py then fails with that instruction rather than guessing.
DEFAULT_RESUME_PATH = _path(_user.get("resume_path"))

# Highest number of bullets any one section may show. `bootstrap` writes the
# counts it actually parsed out of your résumé, because those counts are what
# made your résumé fit on one page.
MAX_BULLETS_PER_SECTION = _user.get("max_bullets_per_section") or {}
MAX_TOTAL_BULLETS = sum(MAX_BULLETS_PER_SECTION.values()) if MAX_BULLETS_PER_SECTION else None

# How a posting is tailored.
#   "minimal": start from the résumé you already approved (base_resume.yaml +
#              BASE_RESUME_PATH), swap wording to the posting's vocabulary, and
#              replace one or two bullets. Sentences are not rewritten.
#   "rebuild": re-pick every bullet from the database and rewrite them all. The
#              older path. It is off by default because compressing facts into a
#              character budget reliably produced telegraphic sentences that lost
#              the why and the impact. Kept for comparison.
TAILOR_MODE = _user.get("tailor_mode") or "minimal"

# The approved résumé that "minimal" mode edits. Required in that mode, and with
# no default on purpose: pointing this at the wrong file is how you would send
# out someone else's résumé.
BASE_RESUME_PATH = _path(_user.get("base_resume_path"))

# Optional. A folder — a cloud-drive sync folder, say — that finished drafts are
# copied into. Missing folder means the copy is skipped quietly; it never blocks
# the run.
SYNC_DIR = _path(_user.get("sync_dir"))

# ── LLM backends ────────────────────────────────────────────────────────────
#
# See llm_client.py for what the roles mean. "auto" picks from what this machine
# actually has, which is why a laptop without Ollama still works.
#
#   light — classification and extraction. Frequent, cheap, tolerates a small model.
#   heavy — writing and judging résumé sentences. A few calls per posting.
LIGHT_BACKEND = _user.get("light_backend") or "auto"
HEAVY_BACKEND = _user.get("heavy_backend") or "auto"
DEFAULT_BACKEND = LIGHT_BACKEND  # older name, kept for --backend's help text

# Extra backends, as argv. This is how `codex-cli` (or any other agent CLI) is
# defined; see user_config.example.yaml. Configuration rather than code because
# these CLIs change their flags faster than this repo can track them.
BACKENDS = _user.get("backends") or {}


def backend_spec(name):
    return BACKENDS.get(name) or _BUILTIN_BACKENDS.get(name)


def known_backend_names():
    """Every CLI backend name that could work here: the shipped presets plus
    whatever user_config.yaml adds. `run.py doctor` lists these."""
    return list(dict.fromkeys(list(_BUILTIN_BACKENDS) + list(BACKENDS)))


# Shipped defaults for CLIs people commonly have. Unverified against every
# version — `python run.py doctor --probe` sends a real prompt so you can check
# yours, and user_config.yaml overrides any of this.
_BUILTIN_BACKENDS = {
    "codex-cli": {
        "command": ["codex", "exec", "--skip-git-repo-check", "-"],
        "prompt_via": "stdin",
    },
    "gemini-cli": {
        "command": ["gemini", "-p"],
        "prompt_via": "arg",
    },
}

# Model names passed to `claude --model`. An alias ("opus", "sonnet") or a full
# model id. The subscription rate limit is shared with your own interactive
# Claude Code, so the strongest model is asked for only where it changes what
# you submit.
#   HEAVY:  bullet selection and rewriting. Two calls per posting.
#   POLISH: recruiter-perspective review, and the rewrite it implies. The last
#           step before a human reads it; degrades to the fallback under a rate
#           limit instead of dropping the step.
HEAVY_MODEL = "sonnet"
HEAVY_FALLBACK_MODEL = None
POLISH_MODEL = "opus"
POLISH_FALLBACK_MODEL = "sonnet"

# Model id for the metered claude-api backend.
CLAUDE_API_MODEL = "claude-sonnet-5"
CLAUDE_MAX_TOKENS = 8192

# Local model for the ollama backend; vendor/ollama_client.py reads OLLAMA_MODEL
# from the environment too.
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "gpt-oss:20b")

# Generation budget when a LOCAL model has to do the heavy work. The planning
# prompt runs to ~10KB and a reasoning model spends its budget on thinking
# before it answers: with the default budget it returns an empty answer and the
# step is lost. Raise num_ctx further if your model supports it; lower it if the
# machine runs out of memory (the symptom is Ollama killing the request).
OLLAMA_HEAVY_OPTIONS = _user.get("ollama_heavy_options") or {
    "num_ctx": 32768, "num_predict": 8192,
}

# A heavy rewrite genuinely takes minutes. Lower this and you will cancel work
# that was about to succeed.
CLI_TIMEOUT_SECONDS = int(_user.get("cli_timeout_seconds") or 1200)

# ── the approval gate ───────────────────────────────────────────────────────

# Run a posting end to end without a human, when something upstream (a job
# watcher, cron) hands it over. False means you get a notification and run
# `tailor` yourself.
AUTO_DRAFT = bool(_user.get("auto_draft", True))

# Postings waiting for your approve/skip, and the listener's heartbeat. Both
# gitignored: a pending record holds the whole JD.
STATE_DIR = os.path.join(PROJECT_ROOT, "state")
PENDING_CONFIRMATIONS_PATH = os.path.join(STATE_DIR, "pending_confirmations.json")
LISTENER_HEARTBEAT_PATH = os.path.join(STATE_DIR, "listener_heartbeat.json")

# Drop a request nobody pressed a button on after this long. Measured reason:
# unanswered requests accumulated indefinitely, each holding a full JD, and a
# posting from three weeks ago has usually closed anyway. Expiry is swept by
# bot_listener's polling loop; there is no separate cron job.
CONFIRM_TTL_DAYS = int(_user.get("confirm_ttl_days") or 7)

# Batch the approval requests instead of sending one per posting. Sending each
# one immediately turned into 15 messages in a day, which is how notifications
# stop being read. Requests queue up and go out as one digest at these hours
# (local time), buttons still per posting. Empty list restores immediate sends.
DIGEST_HOURS = _user.get("digest_hours") or [9, 20]
# Buttons in a single digest. The rest wait for the next one, best fit first.
DIGEST_MAX_ITEMS = int(_user.get("digest_max_items") or 10)

# Postings the pre-fit judgment tiers below this are not offered an approve/skip
# pair; they get a single "draft it anyway" button and sink in the digest.
# Measured: of 87 approval requests only 13 were answered, and 43 of the
# expired ones were roles the user was never going to apply to.
PREFIT_ASK_MIN_TIER = _user.get("prefit_ask_min_tier") or "mid"

# ── quality gates ───────────────────────────────────────────────────────────

# Whether the recruiter-perspective step runs at all.
RECRUITER_REVIEW = bool(_user.get("recruiter_review", True))

# Whether that step may also REWRITE sentences. Off in minimal mode: this is the
# step that produced unreadable lines like "held to ~20%/~5% vs 60-70% claims".
# The score and the specific objections still go in the report; the wording is
# left to you.
RECRUITER_POLISH = bool(_user.get("recruiter_polish", TAILOR_MODE != "minimal"))

# Mark the report with a warning below this score (out of 10).
RECRUITER_SCORE_WARN = int(_user.get("recruiter_score_warn") or 7)

# ── fetching job postings ───────────────────────────────────────────────────

HTTP_TIMEOUT_SECONDS = 15
HTTP_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
