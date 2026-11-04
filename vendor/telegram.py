"""Telegram sender: four levels of message, and an inline-button keyboard.

Credentials come from the environment only — TG_BOT_TOKEN and TG_CHAT_ID. With
either missing, every send is a no-op and the message still goes to the console,
so the whole pipeline runs fine without Telegram. Telegram is only needed for
the approve/skip buttons (bot_listener.py); the CLI works without it.

Do not put a token in this file. See SETUP.md for how to create the bot.
"""

import json
import os
from datetime import datetime

import requests

BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TG_CHAT_ID", "")

API_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}" if BOT_TOKEN else ""

TIMEOUT_SEC = 5


def is_configured():
    return bool(BOT_TOKEN and CHAT_ID)


def _stamp():
    return datetime.now().strftime("%H:%M:%S")


def send(text, buttons=None):
    """Send one message. Returns True if it was actually sent.

    Failures are swallowed on purpose: a notification that cannot be delivered
    must not take down the resume run that produced it.
    """
    if not is_configured():
        return False
    try:
        payload = {
            "chat_id": CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
        }
        if buttons:
            keyboard, row = [], []
            for label, callback in buttons:
                row.append({"text": label, "callback_data": callback})
                if len(row) >= 3:
                    keyboard.append(row)
                    row = []
            if row:
                keyboard.append(row)
            payload["reply_markup"] = json.dumps({"inline_keyboard": keyboard})
        requests.post(f"{API_BASE}/sendMessage", json=payload, timeout=TIMEOUT_SEC)
        return True
    except Exception:
        return False


def silent(msg):
    """Console only: routine progress."""
    print(f"[{_stamp()}] {msg}", flush=True)


def info(msg, buttons=None):
    print(f"[{_stamp()}] {msg}", flush=True)
    send(msg, buttons=buttons)


def alert(msg, buttons=None):
    print(f"[{_stamp()}] ! {msg}")
    send(f"!! {msg}", buttons=buttons)


def critical(msg, buttons=None):
    print(f"[{_stamp()}] !! {msg}")
    send(f"!!!! {msg}", buttons=buttons)
