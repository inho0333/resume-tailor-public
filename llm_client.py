"""Which model answers which prompt.

The pipeline asks two very different things of an LLM, and they want different
backends:

  light  — classification and extraction (is this posting open to my degree,
           what does this JD ask for, is it worth applying). Runs often, tolerates
           a small model, and should be free if it can be.
  heavy  — writing and judging the sentences that go on your résumé. Runs a few
           times per posting and decides the quality of what you submit.

Pick a backend per role with --backend, or leave it on "auto":

    auto           light: a local model if you have one, else a CLI, else the API
                   heavy: a frontier CLI/API model, falling back to local only as
                          a last resort (and it says so)
    ollama         local model via vendor/ollama_client.py — free, needs `ollama serve`
    claude-cli     `claude -p` through the logged-in Claude Code CLI (subscription)
    codex-cli      any configured agent CLI, e.g. `codex exec` (see user_config.example.yaml)
    claude-api     Anthropic API with ANTHROPIC_API_KEY — metered, no CLI needed

Nothing here assumes Ollama. vendor/ollama_client.py is imported only when the
ollama backend is actually chosen, so a laptop with no local model runs the whole
pipeline on a CLI or the API.
"""

import json
import os
import re

import config


class BackendUnavailable(RuntimeError):
    """No backend can serve this role. The message lists what would fix it."""


def parse_json_loose(raw_text):
    """json.loads, tolerant of a ```json fence and of leading prose.

    Every backend goes through this: small local models wrap JSON in fences, and
    agent CLIs sometimes print a line of commentary first.
    """
    cleaned = (raw_text or "").strip()
    fence = re.search(r"```(?:json)?\s*(.*?)\s*```", cleaned, re.DOTALL)
    if fence:
        cleaned = fence.group(1)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        # Last resort: the outermost {...} in the text.
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start != -1 and end > start:
            return json.loads(cleaned[start:end + 1])
        raise


# ── backends ────────────────────────────────────────────────────────────────


class OllamaBackend:
    """Local model. Free, private, no account. Weakest at writing prose.

    The generation budget is raised for heavy work. A reasoning model spends its
    budget on thinking first and the answer second, so a long prompt with the
    default budget returns an empty answer and `done_reason: length` — measured
    with the edit-planning prompt: 9,500 characters of thinking, zero characters
    of answer. The pipeline survives that (it keeps your approved résumé and
    says why), but the step is lost, so ask for enough room up front.
    """

    name = "ollama"
    is_local = True

    def __init__(self, model=None, options=None):
        from vendor import ollama_client  # imported only if this backend is used
        self._ollama = ollama_client
        self.model = model or ollama_client.DEFAULT_MODEL
        self.options = options
        self.name = f"ollama:{self.model}"

    @staticmethod
    def available(model=None):
        try:
            from vendor import ollama_client
        except ImportError:
            return False
        return ollama_client.is_available(model)

    def complete(self, prompt):
        return self._ollama.chat(prompt, model=self.model, json_mode=True,
                                 options=self.options)


class ClaudeCLIBackend:
    """`claude -p` on the logged-in Claude Code CLI.

    Billed to the Claude subscription rather than the API, but the rate limit is
    shared with your own interactive Claude Code — see vendor/claude_cli.py.
    """

    name = "claude-cli"
    is_local = False

    def __init__(self, model=None, fallback_model=None, tag="resume-tailor"):
        from vendor import claude_cli
        self._cli = claude_cli
        self.model = model
        self.fallback_model = fallback_model
        self.tag = tag
        self.name = f"claude-cli:{model}" if model else "claude-cli"

    @staticmethod
    def available():
        from vendor import claude_cli
        return claude_cli.is_available()

    def complete(self, prompt):
        return self._cli.complete(
            prompt=prompt, tag=self.tag, timeout_sec=config.CLI_TIMEOUT_SECONDS,
            model=self.model, fallback_model=self.fallback_model,
        )


class CommandBackend:
    """Any other agent CLI, configured as argv in user_config.yaml.

    Ships with a `codex-cli` entry for OpenAI Codex. The argv is configuration
    rather than code because these CLIs change their flags; `run.py doctor
    --probe` sends a real prompt through it so you can verify yours.
    """

    is_local = False

    def __init__(self, key, spec=None):
        from vendor import command_cli
        self._cli = command_cli
        spec = spec or config.backend_spec(key) or {}
        self.key = key
        self.name = key
        self.command = list(spec.get("command") or [])
        self.prompt_via = spec.get("prompt_via") or "stdin"
        self.strip_prefix_lines = int(spec.get("strip_prefix_lines") or 0)
        if not self.command:
            raise BackendUnavailable(
                f"Backend \"{key}\" has no command configured. Add it to user_config.yaml:\n"
                "  backends:\n"
                f"    {key}:\n"
                "      command: [codex, exec, --skip-git-repo-check, \"-\"]\n"
                "      prompt_via: stdin\n"
                "See user_config.example.yaml.")

    @staticmethod
    def available(key):
        from vendor import command_cli
        spec = config.backend_spec(key) or {}
        command = spec.get("command") or []
        return bool(command) and command_cli.is_available(command)

    def complete(self, prompt):
        return self._cli.complete(
            prompt, command=self.command, prompt_via=self.prompt_via,
            timeout_sec=config.CLI_TIMEOUT_SECONDS, tag=self.key,
            strip_prefix_lines=self.strip_prefix_lines,
        )


class ClaudeAPIBackend:
    """Anthropic API, metered per token. Needs ANTHROPIC_API_KEY in the
    environment — never a key committed to a file in this repo."""

    name = "claude-api"
    is_local = False

    def __init__(self, model=None):
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise BackendUnavailable(
                "ANTHROPIC_API_KEY is not set, which the claude-api backend needs.\n"
                "  export ANTHROPIC_API_KEY=sk-ant-...\n"
                "This backend is metered per token. `--backend claude-cli` uses a Claude "
                "subscription instead, and `--backend ollama` is free and local.")
        try:
            import anthropic
        except ImportError as e:
            raise BackendUnavailable(
                "The anthropic package is not installed: pip install -r requirements.txt"
            ) from e
        self._client = anthropic.Anthropic(api_key=api_key)
        self.model = model or config.CLAUDE_API_MODEL
        self.name = f"claude-api:{self.model}"

    @staticmethod
    def available():
        if not os.environ.get("ANTHROPIC_API_KEY"):
            return False
        try:
            import anthropic  # noqa: F401
        except ImportError:
            return False
        return True

    def complete(self, prompt):
        message = self._client.messages.create(
            model=self.model,
            max_tokens=config.CLAUDE_MAX_TOKENS,
            messages=[{"role": "user", "content": prompt}],
        )
        return "".join(block.text for block in message.content
                       if getattr(block, "type", None) == "text").strip()


# ── selection ───────────────────────────────────────────────────────────────

# Canonical names, plus the aliases earlier versions and other docs use.
_ALIASES = {
    "claude": "claude-cli",
    "claude-subscription": "claude-cli",
    "codex": "codex-cli",
    "local": "ollama",
}

# Order "auto" tries, per role. Light work prefers free and local; heavy work
# prefers the strongest writer available and only falls back to local with a
# warning, because this is the step that produces what you submit.
_AUTO_ORDER = {
    "light": ("ollama", "claude-cli", "codex-cli", "claude-api"),
    "heavy": ("claude-cli", "codex-cli", "claude-api", "ollama"),
}

_resolved = {}


def _available(name):
    if name == "ollama":
        return OllamaBackend.available()
    if name == "claude-cli":
        return ClaudeCLIBackend.available()
    if name == "claude-api":
        return ClaudeAPIBackend.available()
    return CommandBackend.available(name)


def available_backends():
    """{name: bool} for everything configured. Used by `run.py doctor`."""
    names = ["ollama", "claude-cli", "claude-api"] + config.known_backend_names()
    seen, out = set(), {}
    for name in names:
        if name in seen:
            continue
        seen.add(name)
        try:
            out[name] = bool(_available(name))
        except Exception:
            out[name] = False
    return out


def resolve_name(name=None, role="light", log=None):
    """Turn a requested backend (or "auto") into a concrete backend name."""
    name = _ALIASES.get(name or "", name or "")
    if not name:
        configured = config.HEAVY_BACKEND if role == "heavy" else config.LIGHT_BACKEND
        name = _ALIASES.get(configured, configured)
    if name != "auto":
        return name

    cached = _resolved.get(role)
    if cached:
        return cached
    for candidate in _AUTO_ORDER[role]:
        if _available(candidate):
            if role == "heavy" and candidate == "ollama" and log:
                log("  [backend] no CLI or API available, so the résumé sentences are being "
                    "written by a local model. Expect weaker prose; `claude-cli` or "
                    "`claude-api` is strongly preferred for this step.")
            _resolved[role] = candidate
            if log:
                log(f"  [backend] auto-selected {candidate} for {role} work")
            return candidate

    raise BackendUnavailable(
        f"No LLM backend is available for {role} work. Any one of these fixes it:\n"
        "  • Ollama (free, local):   ollama serve && ollama pull "
        f"{config.OLLAMA_MODEL}\n"
        "  • Claude Code CLI:        install Claude Code and run `claude` once to log in\n"
        "  • Another agent CLI:      configure `backends:` in user_config.yaml "
        "(see user_config.example.yaml)\n"
        "  • Anthropic API:          export ANTHROPIC_API_KEY=sk-ant-...\n"
        "Run `python run.py doctor` for what this machine currently has.")


def get_backend(name=None, role="light", log=None, model=None, fallback_model=None,
                tag="resume-tailor"):
    """A backend object with .name and .complete(prompt) -> str."""
    resolved = resolve_name(name, role=role, log=log)
    if resolved == "ollama":
        return OllamaBackend(options=config.OLLAMA_HEAVY_OPTIONS if role == "heavy" else None)
    if resolved == "claude-cli":
        return ClaudeCLIBackend(model=model, fallback_model=fallback_model, tag=tag)
    if resolved == "claude-api":
        return ClaudeAPIBackend(model=model)
    return CommandBackend(resolved)


def get_heavy_backend(name=None, log=None):
    """Bullet selection and rewriting — the sentences you submit."""
    return get_backend(name, role="heavy", log=log,
                       model=config.HEAVY_MODEL, fallback_model=config.HEAVY_FALLBACK_MODEL,
                       tag="resume-tailor")


def get_polish_backend(name=None, log=None):
    """Recruiter-perspective review. The highest quality bar in the pipeline, so
    it asks for the strongest model and degrades to a fallback rather than
    dropping the step."""
    return get_backend(name, role="heavy", log=log,
                       model=config.POLISH_MODEL, fallback_model=config.POLISH_FALLBACK_MODEL,
                       tag="resume-tailor-recruiter")
