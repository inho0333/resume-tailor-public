"""Discord, through an incoming webhook.

The quickest channel to set up: Server Settings -> Integrations -> Webhooks
gives you one URL, and that is the whole configuration. The trade is that a
webhook is one-way — it cannot carry buttons and nothing can answer through it —
so requests arrive with the CLI command that answers them.

Set DISCORD_WEBHOOK_URL. A bot token would buy buttons, but it also buys a
gateway connection and a second long-running process; the webhook is the version
that matches what this channel is for.
"""

import os

import requests

from channels import base

# Discord rejects a message over 2000 characters outright, so a long digest is
# split rather than truncated: the postings at the bottom of a digest are the
# ones already sorted as least interesting, but they are still questions.
MESSAGE_LIMIT = 1900


def _split(text, limit=MESSAGE_LIMIT):
    chunks, current = [], []
    length = 0
    for block in text.split("\n\n"):
        if length + len(block) + 2 > limit and current:
            chunks.append("\n\n".join(current))
            current, length = [], 0
        current.append(block)
        length += len(block) + 2
    if current:
        chunks.append("\n\n".join(current))
    return chunks or [""]


class Channel(base.Channel):
    name = "discord"

    def __init__(self):
        self.url = os.environ.get("DISCORD_WEBHOOK_URL", "")

    def is_configured(self):
        return bool(self.url)

    def status_line(self):
        if self.is_configured():
            return "ready — webhook posts, answered from the CLI (no buttons)"
        return ("set DISCORD_WEBHOOK_URL (Server Settings -> Integrations -> Webhooks); "
                "no buttons, so approvals are typed")

    def _send_text(self, text, keyboard=None):
        sent = True
        for chunk in _split(base.to_plain(text)):
            try:
                resp = requests.post(self.url, json={"content": chunk}, timeout=20)
                resp.raise_for_status()
            except requests.RequestException as e:
                print(f"[discord send failed] {e}")
                sent = False
        return sent

    def send_document(self, path, caption=""):
        if not self.is_configured():
            return False
        if not os.path.exists(path):
            return self.send_message(f"{caption}\n\n(the draft file is missing: {path})")
        try:
            with open(path, "rb") as f:
                resp = requests.post(
                    self.url,
                    data={"content": base.to_plain(caption)[:MESSAGE_LIMIT]},
                    files={"file": (os.path.basename(path), f)},
                    timeout=30,
                )
            resp.raise_for_status()
            return True
        except requests.RequestException as e:
            print(f"[discord] document upload failed: {e}")
            return False
