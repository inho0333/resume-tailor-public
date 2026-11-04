"""Headless Claude Code (`claude -p`) wrapper.

`claude -p` authenticates with whatever session the `claude` CLI is logged into.
On a Claude subscription that means the call is billed to the subscription and
not to the pay-as-you-go API — but note that the subscription's rate limit is
**shared with the interactive Claude Code you use yourself**: if you exhaust the
window by hand, calls from this tool fail until it resets. That is what
RateLimitError is for; it is congestion, not a bug.

ANTHROPIC_API_KEY is removed from the child environment on purpose. If the
variable is set, the CLI may bill the API instead of the subscription, which is
the one failure mode that silently costs money.
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

DEFAULT_TIMEOUT_SEC = 600

# Retries for a transient failure. The empirical case for them: of 181 calls
# logged over three weeks, 10 failed, and 8 of those were an exit 1 with both
# stdout and stderr empty. Every one of those succeeded minutes later with the
# same prompt, yet the code gave up the whole pipeline on the first failure.
DEFAULT_RETRIES = 2
_RETRY_BACKOFF_SEC = (20, 60)

_TRANSIENT_MARKERS = (
    "bad gateway", "502", "503", "504", "gateway timeout", "service unavailable",
    "connection reset", "econnreset", "etimedout", "socket hang up", "fetch failed",
    "internal server error", "overloaded",
)

# A launchd job inherits PATH=/usr/bin:/bin:/usr/sbin:/sbin — no /opt/homebrew/bin.
# The same call then works in your shell and fails under launchd with "claude CLI
# not found". Search these explicitly and pass them to the child as well.
_EXTRA_PATH = "/opt/homebrew/bin:/usr/local/bin"
_SEARCH_PATH = f"{_EXTRA_PATH}:{os.environ.get('PATH', '')}"

# Usage log, so you can see how often this touches your subscription. Lives under
# state/ (gitignored). Logging never blocks a call: if it fails, it is skipped.
_LOG_FILE = os.environ.get("RESUME_TAILOR_CLI_LOG") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "state", "cli_usage_log.json")


def _claude_bin():
    """Path to the claude executable, or None. Works under a short launchd PATH."""
    return shutil.which("claude", path=_SEARCH_PATH)


def is_available():
    return _claude_bin() is not None


class EscalationError(Exception):
    pass


class RateLimitError(EscalationError):
    """The subscription window is exhausted — congestion, not a defect.

    Callers should treat this differently from EscalationError: skip quietly or
    wait, do not raise an alarm. `reset_at` is the reset time parsed out of the
    message (tz-aware datetime), or None if it could not be read.
    """

    def __init__(self, message, reset_at=None):
        super().__init__(message)
        self.reset_at = reset_at


def _parse_reset_time(detail):
    """'resets 8:50am (America/Los_Angeles)' -> a tz-aware datetime."""
    m = re.search(r"resets\s+(\d{1,2}):(\d{2})\s*(am|pm)\s*\(([^)]+)\)", detail, re.I)
    if not m:
        return None
    hour, minute, ampm, tzname = int(m.group(1)), int(m.group(2)), m.group(3).lower(), m.group(4)
    if ampm == "pm" and hour != 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0
    try:
        tz = ZoneInfo(tzname.strip())
    except Exception:
        return None
    now = datetime.now(tz)
    reset = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if reset <= now:  # the reset is past midnight
        reset += timedelta(days=1)
    return reset


def is_rate_limit(detail):
    """Is this message a usage limit? Matched loosely; the wording changes."""
    d = (detail or "").lower()
    return any(k in d for k in ("session limit", "usage limit", "rate limit", "resets "))


_EMPTY_OUTPUT_DETAIL = ("both stdout and stderr were empty — possibly a usage limit, or the "
                        "claude CLI is logged out. Run `claude -p 'hi'` by hand to tell which")


def is_transient(detail):
    """Worth retrying? Empty output counts: see DEFAULT_RETRIES."""
    d = (detail or "").strip().lower()
    if not d or d == _EMPTY_OUTPUT_DETAIL.lower():
        return True
    return any(k in d for k in _TRANSIENT_MARKERS)


def _atomic_write(filepath, data):
    dir_path = os.path.dirname(filepath)
    os.makedirs(dir_path, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=dir_path, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False, default=str)
        os.replace(tmp_path, filepath)
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


def _log_call(tag, prompt_len, ok, error=None):
    logs = []
    try:
        if os.path.exists(_LOG_FILE):
            with open(_LOG_FILE, "r", encoding="utf-8") as f:
                logs = json.load(f)
        logs.append({
            "timestamp": datetime.now().isoformat(),
            "tag": tag,
            "prompt_len": prompt_len,
            "ok": ok,
            "error": error,
        })
        _atomic_write(_LOG_FILE, logs)
    except (OSError, json.JSONDecodeError, ValueError):
        pass  # the usage log is a convenience, never a reason to fail a call


def complete(prompt=None, prompt_file=None, timeout_sec=DEFAULT_TIMEOUT_SEC, tag="unspecified",
             disallowed_tools=None, model=None, fallback_model=None, retries=DEFAULT_RETRIES,
             log=print):
    """Run `claude -p` with the prompt and return the text response.

    model/fallback_model map to `--model`/`--fallback-model` and take an alias
    ("opus", "sonnet") or a full model name. Unset means the CLI default, i.e.
    whatever model you use interactively.

    A usage limit raises RateLimitError immediately and is never retried; the
    caller has to decide whether to wait.
    """
    claude_bin = _claude_bin()
    if not claude_bin:
        raise EscalationError(
            "claude CLI not found. Install Claude Code and log in (`claude`), or pick another "
            f"backend with --backend. Searched: {_SEARCH_PATH}"
        )

    if prompt_file and not prompt:
        with open(prompt_file, "r", encoding="utf-8") as f:
            prompt = f.read()
    if not prompt:
        raise ValueError("one of prompt or prompt_file is required")

    env = dict(os.environ)
    env.pop("ANTHROPIC_API_KEY", None)  # keep the call on the subscription, not the API
    env["PATH"] = _SEARCH_PATH          # claude looks up node and friends on PATH

    cmd = [claude_bin, "-p", prompt, "--output-format", "text"]
    if disallowed_tools:
        cmd += ["--disallowedTools", ",".join(disallowed_tools)]
    if model:
        cmd += ["--model", model]
    if fallback_model:
        cmd += ["--fallback-model", fallback_model]

    attempt = 0
    while True:
        attempt += 1
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout_sec,
                env=env,
                stdin=subprocess.DEVNULL,  # the prompt is already an argument; do not let the
                                           # caller's stdin get inherited and read
            )
        except subprocess.TimeoutExpired as e:
            # Not retried: this already waited out the full timeout once, and the
            # same prompt would wait exactly as long again.
            _log_call(tag, len(prompt), ok=False, error=f"timeout after {timeout_sec}s")
            raise EscalationError(f"claude -p timed out ({timeout_sec}s, tag={tag})") from e

        if result.returncode == 0:
            _log_call(tag, len(prompt), ok=True)
            return result.stdout.strip()

        detail = (result.stderr or "").strip()[:500]
        if not detail:
            detail = (result.stdout or "").strip()[:500]
        if not detail:
            detail = _EMPTY_OUTPUT_DETAIL
        _log_call(tag, len(prompt), ok=False, error=detail)
        if is_rate_limit(detail):
            raise RateLimitError(
                f"claude -p hit a usage limit (tag={tag}): {detail}",
                reset_at=_parse_reset_time(detail),
            )
        if attempt <= retries and is_transient(detail):
            wait = _RETRY_BACKOFF_SEC[min(attempt - 1, len(_RETRY_BACKOFF_SEC) - 1)]
            if log:
                log(f"  [claude retry {attempt}/{retries}] retrying in {wait}s "
                    f"(tag={tag}, cause: {detail[:80]})")
            time.sleep(wait)
            continue
        suffix = f" — all {attempt} attempts failed" if attempt > 1 else ""
        raise EscalationError(
            f"claude -p failed (exit {result.returncode}, tag={tag}){suffix}: {detail}")


def usage_summary():
    """Call counts by tag, so you can check this is not running more than you think."""
    if not os.path.exists(_LOG_FILE):
        return {"total_calls": 0, "by_tag": {}}
    try:
        with open(_LOG_FILE, "r", encoding="utf-8") as f:
            logs = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {"total_calls": 0, "by_tag": {}}
    by_tag = {}
    for entry in logs:
        by_tag[entry["tag"]] = by_tag.get(entry["tag"], 0) + 1
    return {"total_calls": len(logs), "by_tag": by_tag}
