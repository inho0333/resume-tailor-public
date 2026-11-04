"""Local Ollama client. Plain `requests`, no SDK.

Ollama exposes an OpenAI-compatible endpoint, but this uses the native
/api/chat: streaming off, and an option to force a JSON response.

Optional. Nothing here is imported unless the `ollama` backend is actually
selected (see llm_client.py), so the tool runs on a machine without Ollama.
"""

import json
import os
import re

import requests

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
DEFAULT_MODEL = os.environ.get("OLLAMA_MODEL", "gpt-oss:20b")
DEFAULT_TIMEOUT = 120

# Do not lower these without reading the next paragraph.
#
# A reasoning model (gpt-oss and friends) spends its generation budget on
# `message.thinking` first and puts the answer in `message.content`. With the
# model default context (usually 4096) a long prompt leaves no budget for the
# answer, and the call returns content="" with done_reason="length" — not an
# error, just an empty string. Measured with the same prompt: model defaults ->
# content 0 chars; num_ctx 8192 + num_predict 2048 -> valid JSON.
#
# Turning thinking off (think=False) is the other way out, but response quality
# dropped visibly, so the budget is raised instead. A caller's own `options`
# still win over these.
DEFAULT_OPTIONS = {"num_ctx": 8192, "num_predict": 2048}


class OllamaError(Exception):
    pass


def chat(prompt, system=None, model=None, json_mode=False, timeout=DEFAULT_TIMEOUT,
         options=None):
    """Send one user message, return the text response.

    json_mode=True sets Ollama's format="json", which constrains the output to
    valid JSON syntax. It does not guarantee the JSON is *complete* — a reply
    truncated by the generation budget is still broken JSON — so parse the
    result with parse_json_loose() and handle the exception.

    options: native Ollama options (`num_ctx`, `num_predict`, `temperature`, …).
    Merged over DEFAULT_OPTIONS.
    """
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    payload = {
        "model": model or DEFAULT_MODEL,
        "messages": messages,
        "stream": False,
    }
    if json_mode:
        payload["format"] = "json"
    payload["options"] = {**DEFAULT_OPTIONS, **(options or {})}

    try:
        resp = requests.post(f"{OLLAMA_HOST}/api/chat", json=payload, timeout=timeout)
        resp.raise_for_status()
    except requests.RequestException as e:
        raise OllamaError(
            f"Ollama call failed ({OLLAMA_HOST}). Is `ollama serve` running, and have you "
            f"run `ollama pull {model or DEFAULT_MODEL}`? Underlying error: {e}"
        ) from e

    data = resp.json()
    message = data.get("message", {})
    content = message.get("content", "")
    if not content:
        thinking_len = len(message.get("thinking") or "")
        if thinking_len and data.get("done_reason") == "length":
            raise OllamaError(
                f"The model produced {thinking_len} characters of thinking and an empty "
                f"answer: it ran out of generation budget. Raise num_predict/num_ctx, or "
                f"use a smaller prompt "
                f"(model={data.get('model')}, eval_count={data.get('eval_count')})"
            )
        raise OllamaError(f"No content in the Ollama response: {data}")
    return content


def parse_json_loose(raw_text):
    """json.loads, but tolerant of a ```json ... ``` fence around the object."""
    cleaned = (raw_text or "").strip()
    fence_match = re.search(r"```(?:json)?\s*(.*?)\s*```", cleaned, re.DOTALL)
    if fence_match:
        cleaned = fence_match.group(1)
    return json.loads(cleaned)


def is_available(model=None, timeout=5):
    """True if Ollama is reachable and has the model pulled.

    Used by the `auto` backend to decide whether a local model is an option at
    all, and by `run.py doctor`.
    """
    try:
        resp = requests.get(f"{OLLAMA_HOST}/api/tags", timeout=timeout)
        resp.raise_for_status()
        tags = [m.get("model", "") for m in resp.json().get("models", [])]
        target = model or DEFAULT_MODEL
        return any(t == target or t.startswith(target.split(":")[0]) for t in tags)
    except requests.RequestException:
        return False
