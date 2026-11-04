"""Run any coding-agent CLI as an LLM backend.

This exists so the tool is not tied to one vendor. If a CLI takes a prompt and
prints an answer, it can drive this pipeline: OpenAI Codex (`codex exec`),
Gemini CLI, or your own wrapper script. Configure the argv in user_config.yaml:

    backends:
      codex-cli:
        command: [codex, exec, --skip-git-repo-check, "-"]
        prompt_via: stdin       # or "arg" to append the prompt as the last argv

Why configurable instead of hardcoded: these CLIs change their flags, and this
project cannot test against all of them. `python run.py doctor --probe` sends a
one-line prompt through whichever backend you configured and shows you the raw
result, so you can fix the argv yourself rather than read someone's guess.
"""

import os
import re
import shutil
import subprocess

DEFAULT_TIMEOUT_SEC = 600

_EXTRA_PATH = "/opt/homebrew/bin:/usr/local/bin"
_SEARCH_PATH = f"{_EXTRA_PATH}:{os.environ.get('PATH', '')}"

# Agent CLIs like to decorate stdout with colour and spinner frames. The pipeline
# feeds this output to json.loads, so strip the escape sequences.
_ANSI_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


class CommandBackendError(Exception):
    pass


def resolve(command):
    """Absolute path of the executable in `command`, or None if not installed."""
    if not command:
        return None
    return shutil.which(command[0], path=_SEARCH_PATH)


def is_available(command):
    return resolve(command) is not None


def complete(prompt, command, prompt_via="stdin", timeout_sec=DEFAULT_TIMEOUT_SEC,
             tag="unspecified", strip_prefix_lines=0, log=print):
    """Send the prompt through `command` and return its stdout.

    prompt_via: "stdin" pipes the prompt in; "arg" appends it as the last argv.
    strip_prefix_lines: drop this many leading lines of banner output (some CLIs
    print a session header before the answer).
    """
    binary = resolve(command)
    if not binary:
        raise CommandBackendError(
            f"`{command[0]}` not found on PATH. Install it, fix `backends` in "
            f"user_config.yaml, or choose another --backend. Searched: {_SEARCH_PATH}")

    argv = [binary] + list(command[1:])
    stdin_text = None
    if prompt_via == "arg":
        argv.append(prompt)
    else:
        stdin_text = prompt

    env = dict(os.environ)
    env["PATH"] = _SEARCH_PATH
    env.setdefault("NO_COLOR", "1")
    env.setdefault("TERM", "dumb")

    try:
        result = subprocess.run(
            argv, input=stdin_text, capture_output=True, text=True,
            timeout=timeout_sec, env=env,
        )
    except subprocess.TimeoutExpired as e:
        raise CommandBackendError(
            f"{command[0]} timed out ({timeout_sec}s, tag={tag})") from e

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()[:500]
        raise CommandBackendError(
            f"{command[0]} failed (exit {result.returncode}, tag={tag}): {detail}")

    out = _ANSI_RE.sub("", result.stdout or "").strip()
    if strip_prefix_lines:
        out = "\n".join(out.splitlines()[strip_prefix_lines:]).strip()
    if not out:
        raise CommandBackendError(
            f"{command[0]} returned no output (tag={tag}). Run `python run.py doctor --probe` "
            f"to see the raw call.")
    return out
