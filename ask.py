"""Asking the human something, safely.

Every interactive prompt in this project goes through here, because a bare
`input()` raises EOFError the moment there is no terminal — piped stdin, a cron
job, a launchd daemon, `python run.py tailor < /dev/null`. That turned an
ordinary unattended run into a traceback after the expensive LLM calls had
already been made and paid for.

With no terminal, each helper returns the default it was given and says so, so a
non-interactive run behaves exactly like `--auto`.

## Except where a default is not an answer

That rule is right for `tailor`, where every default is "carry on", and wrong
for `run.py setup`, where the defaults are empty. Piping answers into setup —
from a script, a container build, or an IDE terminal that allocates no tty —
silently skipped every question, wrote a `profile.yaml` whose name and career
summary were empty strings, and finished by printing "Nothing is missing."

So a caller that cannot work without real answers says so with
`require_answers()`. Prompts then read stdin whether or not it is a terminal,
which works for a pipe, and still fall back to the default at end of input
rather than raising.
"""

import sys

_require_answers = False


def require_answers(flag=True):
    """Read stdin even when it is not a terminal.

    For `run.py setup`, whose defaults are empty values rather than safe ones.
    Never set this in a path that may run unattended.
    """
    global _require_answers
    _require_answers = bool(flag)


def _interactive():
    if _require_answers:
        return True
    try:
        return sys.stdin is not None and sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def confirm(prompt, default=True, log=print):
    """Yes/no. Returns `default` when there is no terminal.

    The default is what an unattended run does, so set it to the answer that is
    safe to take without a human: proceeding is safe here (nothing is submitted
    anywhere), cancelling would waste the work already done.
    """
    if not _interactive():
        log(f"  [no terminal] {prompt.strip()} -> {'yes' if default else 'no'}")
        return default
    try:
        answer = input(prompt).strip().lower()
    except (EOFError, KeyboardInterrupt):
        log("  [input cancelled] " + ("proceeding" if default else "stopping"))
        return default
    if not answer:
        return default
    return answer in ("y", "yes")


def line(prompt, default="", log=print):
    """A free-text answer. Returns `default` (normally empty = skip) with no terminal."""
    if not _interactive():
        return default
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        log("  [input cancelled] skipping")
        return default
