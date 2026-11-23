"""Telegram: the only channel that can answer its own messages.

Credentials come from the environment only — TG_BOT_TOKEN and TG_CHAT_ID. This
wraps vendor/telegram.py, which is the raw sender, and adds the two things the
pipeline needs: a row-by-row keyboard (so a posting's ✅/⏭️ pair stays on one
row) and document upload.
"""

import json
import os

import requests

from channels import base
from vendor import telegram as _raw

CAPTION_LIMIT = 1000  # Telegram's sendDocument caption limit, 24 characters short.


class Channel(base.Channel):
    name = "telegram"
    supports_buttons = True

    def is_configured(self):
        return _raw.is_configured()

    def status_line(self):
        if self.is_configured():
            return "ready — inline buttons, answered in the chat"
        return ("set TG_BOT_TOKEN and TG_CHAT_ID (create a bot with @BotFather; "
                "`run.py setup` walks through it)")

    def _send_text(self, text, keyboard=None):
        if not self.is_configured():
            return False
        payload = {"chat_id": _raw.CHAT_ID, "text": text, "parse_mode": "HTML",
                   "disable_web_page_preview": True}
        if keyboard:
            # Row by row, not three-to-a-row as vendor/telegram.send does: a
            # digest carries up to ten postings and two buttons each, and a pair
            # split across rows leaves you unable to tell which posting a button
            # belongs to.
            payload["reply_markup"] = json.dumps({
                "inline_keyboard": [[{"text": label, "callback_data": cb} for label, cb in row]
                                    for row in keyboard]})
        try:
            resp = requests.post(f"{_raw.API_BASE}/sendMessage", json=payload, timeout=20)
            resp.raise_for_status()
            return True
        except requests.RequestException as e:
            # Log the response body too — on a 400 (HTML parsing, caption length)
            # it is the only way to find which character caused it.
            detail = getattr(getattr(e, "response", None), "text", "")[:300]
            print(f"[telegram send failed] {e} {detail}\n{text[:500]}")
            return False

    def send_document(self, path, caption=""):
        if not self.is_configured():
            return False
        if not os.path.exists(path):
            print(f"[telegram] document upload failed — no such file: {path}")
            return False
        try:
            with open(path, "rb") as f:
                resp = requests.post(
                    f"{_raw.API_BASE}/sendDocument",
                    data={"chat_id": _raw.CHAT_ID, "caption": caption, "parse_mode": "HTML"},
                    files={"document": f},
                    timeout=30,
                )
            resp.raise_for_status()
            return True
        except requests.RequestException as e:
            print(f"[telegram] document upload failed: {e}")
            return False
