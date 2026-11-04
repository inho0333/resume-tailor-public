"""Daemon that stays up waiting for the "confirm before writing" buttons
(✅ Make the resume / ⏭️ Skip).

Unlike `run.py tailor --auto` (a 2-minute poll, triggered by an upstream job
watcher that is not part of this repo), this one has to be running at all times,
so it runs as its own launchd job with KeepAlive — see
`com.local.confirm-bot-listener.plist`.

It is the usual shape for a Telegram listener: getUpdates long-poll, one thread
per callback. Only ONE poller may hold a given bot token — two getUpdates pollers
on the same token get 409 Conflict from Telegram, so this dedicated listener must
be the only process polling it.

Telegram credentials come from the environment only (TG_BOT_TOKEN / TG_CHAT_ID)
by way of `vendor/telegram.py`; nothing reads a committed secrets file. The
listener lives in this repo rather than next to the sender because handling a
callback (`confirm_handler.py`) needs python-docx and PyYAML — running it in the
virtualenv that already has those dependencies was the practical choice."""

import datetime
import json
import os
import re
import shutil
import socket
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import log_stamp

# Stamp every log line with date and time (launchd writes stdout straight to a file).
log_stamp.install()

socket.setdefaulttimeout(60)  # Same reason as run.py — never wait forever.

import requests

import config
import confirm_handler
import confirm_state
import decisions
import digest
import genlock
import notify
from vendor import telegram

POLL_TIMEOUT_SEC = 25
# How often expired pending requests are pruned. One poll is 25 seconds, so there is
# no reason to read and write the file every loop, and the TTL is measured in days —
# checking on the hour is plenty.
PRUNE_INTERVAL_SEC = 3600
# How often the digest send time is checked. The send itself is deduplicated by the
# hour (digest.send_if_due), so checking often is harmless — one minute keeps it
# going out within a few minutes of the hour.
DIGEST_CHECK_INTERVAL_SEC = 60
# How often the liveness signal is written. The upstream watcher calls the listener
# "stuck" after 15 minutes, so this must be far shorter than that.
HEARTBEAT_INTERVAL_SEC = 60
# How often source changes are checked. See the _source_changed docstring below.
SOURCE_CHECK_INTERVAL_SEC = 60
_OFFSET_PATH = os.path.join(config.STATE_DIR, "telegram_offset.json")

# launchd appends stdout to this file. Renaming the file does not help: the fd follows
# the rename (launchd never reopens it), so the only usable strategy is **copy, then
# truncate in place** (copytruncate).
_LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "bot_listener.log")
_LOG_MAX_BYTES = 5 * 1024 * 1024

_BOT_TOKEN = telegram.BOT_TOKEN
_CHAT_ID = str(telegram.CHAT_ID)


def _load_offset():
    if not os.path.exists(_OFFSET_PATH):
        return 0
    with open(_OFFSET_PATH, "r", encoding="utf-8") as f:
        return json.load(f).get("offset", 0)


def _save_offset(offset):
    os.makedirs(config.STATE_DIR, exist_ok=True)
    tmp = _OFFSET_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"offset": offset}, f)
    os.replace(tmp, _OFFSET_PATH)


def _answer_callback(callback_id, text):
    try:
        requests.post(
            f"https://api.telegram.org/bot{_BOT_TOKEN}/answerCallbackQuery",
            json={"callback_query_id": callback_id, "text": text},
            timeout=10,
        )
    except requests.RequestException as e:
        print(f"[bot_listener] answerCallbackQuery failed: {e}")


def _process_update(update):
    cq = update.get("callback_query")
    if not cq:
        return  # Plain (text) messages are not handled by this bot yet — buttons only.

    from_id = str(cq.get("from", {}).get("id", ""))
    if from_id != _CHAT_ID:
        _answer_callback(cq["id"], "Not authorized")
        return

    data = cq.get("data", "")
    if data.startswith("jw:make:"):
        _answer_callback(cq["id"], "Writing the resume — one moment")
        short_id = data[len("jw:make:"):]
        threading.Thread(target=confirm_handler.handle_make, args=(short_id,), daemon=True).start()
    elif data.startswith("jw:skip:"):
        _answer_callback(cq["id"], "Skipped")
        short_id = data[len("jw:skip:"):]
        threading.Thread(target=confirm_handler.handle_skip, args=(short_id,), daemon=True).start()
    else:
        _answer_callback(cq["id"], "")


def _poll_once():
    r = requests.get(
        f"https://api.telegram.org/bot{_BOT_TOKEN}/getUpdates",
        params={
            "offset": _load_offset(), "timeout": POLL_TIMEOUT_SEC,
            "allowed_updates": '["callback_query"]',
        },
        timeout=POLL_TIMEOUT_SEC + 10,
    )
    r.raise_for_status()
    result = r.json().get("result", [])
    offset = _load_offset()
    for update in result:
        _process_update(update)
        offset = max(offset, update["update_id"] + 1)
    if result:
        _save_offset(offset)


def _prune_expired():
    """Clean up expired "confirm before writing" requests. Nothing goes to Telegram —
    these are postings the user left untouched for weeks, so a notification now would
    be noise about something not worth reviving (the console log keeps the record).

    An expiry is **booked into the decision journal as an implicit no**: not pressing
    a button for seven days is the strongest signal the upstream filter could learn
    from (43 expiries measured), and it used to drain away as a single log line."""
    try:
        for record in confirm_state.prune_expired():
            company, role = record.get("company", "?"), record.get("role", "?")
            notify.silent(
                f"[confirm] pruned after {config.CONFIRM_TTL_DAYS}d with no answer: "
                f"{company} · {role}")
            decisions.record("expire", record, source="prune")
    except Exception as e:
        # Even if pruning fails, button handling (this daemon's real job) must keep running.
        print(f"[bot_listener] pending-request pruning failed: {e}")


def _write_heartbeat():
    """Liveness signal. An upstream job watcher (a 2-minute poll in **its own
    virtualenv**, not part of this repo) raises an alert when the ts in this file stops
    advancing — once an auto-sync broke this project's virtualenv and the listener
    failed to poll 11 times in a row with a certifi error, and nothing anywhere told
    the user. When the listener is dead, pressing ✅ does nothing at all and there is
    no way to find that out."""
    try:
        os.makedirs(config.STATE_DIR, exist_ok=True)
        tmp = config.LISTENER_HEARTBEAT_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({
                "ts": datetime.datetime.now().isoformat(timespec="seconds"),
                "pid": os.getpid(),
                "pending": len(confirm_state.all_records()),
            }, f)
        os.replace(tmp, config.LISTENER_HEARTBEAT_PATH)
    except Exception as e:
        print(f"[bot_listener] heartbeat write failed: {e}")


def _rotate_log():
    """When the log grows, copy it and then truncate in place (copytruncate).

    Measured once: of 107KB, most was the same one-line "polling recovered" message
    repeating, and there was no rotation of any kind. launchd holds the stdout fd, so
    the rename strategy is unusable (it never reopens a new file, so the log keeps
    flowing into a file nobody can see) — hence copy to .1 and truncate the original."""
    try:
        if not os.path.exists(_LOG_PATH) or os.path.getsize(_LOG_PATH) < _LOG_MAX_BYTES:
            return
        shutil.copy2(_LOG_PATH, _LOG_PATH + ".1")
        with open(_LOG_PATH, "r+") as f:
            f.truncate(0)
        print(f"[bot_listener] log rotated: kept as {_LOG_PATH}.1 and emptied")
    except OSError as e:
        print(f"[bot_listener] log rotation failed: {e}")


def _source_mtimes():
    directory = os.path.dirname(os.path.abspath(__file__))
    out = {}
    for name in os.listdir(directory):
        if name.endswith(".py") and not name.endswith((".bak", ".pyc")):
            try:
                out[name] = os.path.getmtime(os.path.join(directory, name))
            except OSError:
                pass
    return out


def _source_changed(baseline):
    """Whether any .py file changed since this process started.

    **Why this is needed (measured once, and it was the worst problem in this
    system):** the daemon started one afternoon and then ran for ten days, during
    which 11 modules changed. Python reads modules into memory at startup, so
    `pagefit.py` (the Word-measured one-page guarantee) added on day nine, and the
    ADDITIONAL label fix, had **never once executed on the button path** — that file
    did not even exist when the daemon came up. Fixing the code does nothing without
    a restart, and nothing told anyone.

    So when the source changes it **exits on its own** — launchd's KeepAlive brings it
    straight back on the new code. Except while a generation is in flight: the threads
    are daemon=True, so exiting would throw that whole generation away and one ✅ the
    user pressed would vanish."""
    current = _source_mtimes()
    changed = [name for name, mtime in current.items()
               if baseline.get(name) != mtime]
    return changed


def _generation_in_flight():
    """Whether a generation thread is running — True if some process holds the genlock
    file. Used to defer a restart/exit until after it finishes."""
    probe = genlock.GenerationLock(label="probe", timeout_sec=0)
    try:
        with probe:
            return False
    except genlock.LockBusy:
        return True
    except OSError:
        return False


_MAX_CONSECUTIVE_FAILURES = 20  # Past this, exit the process — launchd KeepAlive brings
# up a clean one (fresh sockets, fresh DNS cache).

# requests' HTTPError puts the whole request URL in the exception message. The getUpdates
# URL carries the bot token in its path, so printing the exception as-is leaves **the bot
# token in the log in plaintext**. It actually happened once (a single 502 Bad Gateway).
# Strip the token in whatever shape it appears before printing.
_TOKEN_IN_URL_RE = re.compile(r"(api\.telegram\.org/bot)[0-9]{6,}:?[A-Za-z0-9_\-]*")


def _safe_err(e):
    return _TOKEN_IN_URL_RE.sub(r"\1<REDACTED>", str(e))


# Poll timeouts happen normally in long-polling (measured failure rate 0.9%, every one of
# them recovered on the next attempt) and printing each one makes 75% of the log that
# single line — real incidents get buried. While failures are consecutive, just count
# them, and on recovery summarize as one line: "recovered after N failures". Exceeding
# the limit (an exit) is of course still printed.
_QUIET_ERRORS = (requests.exceptions.Timeout, requests.exceptions.ConnectionError)


def main():
    if not (_BOT_TOKEN and _CHAT_ID and _CHAT_ID != "None"):
        print("[bot_listener] Telegram bot token / chat id not set — exiting.")
        sys.exit(1)
    source_baseline = _source_mtimes()
    print(f"[bot_listener] started (pid={os.getpid()}, {len(source_baseline)} modules loaded, "
          f"digest {config.DIGEST_HOURS or 'send immediately'})")

    consecutive_failures = 0
    quiet_failures = 0
    last_prune = 0.0
    last_digest_check = 0.0
    last_heartbeat = 0.0
    last_source_check = time.time()
    while True:
        try:
            now = time.time()
            if now - last_prune >= PRUNE_INTERVAL_SEC:
                _prune_expired()
                _rotate_log()
                last_prune = now
            if now - last_heartbeat >= HEARTBEAT_INTERVAL_SEC:
                _write_heartbeat()
                last_heartbeat = now
            if now - last_digest_check >= DIGEST_CHECK_INTERVAL_SEC:
                try:
                    digest.send_if_due()
                except Exception as e:
                    print(f"[bot_listener] digest send failed: {e}")
                last_digest_check = now
            if now - last_source_check >= SOURCE_CHECK_INTERVAL_SEC:
                last_source_check = now
                changed = _source_changed(source_baseline)
                if changed:
                    if _generation_in_flight():
                        print(f"[bot_listener] code change detected"
                              f"({', '.join(sorted(changed)[:5])}) — a generation is in "
                              f"flight, restarting once it finishes")
                    else:
                        print(f"[bot_listener] code change detected"
                              f"({', '.join(sorted(changed)[:5])}) — exiting to come back "
                              f"on the new code (launchd KeepAlive restarts it)")
                        sys.exit(0)
            _poll_once()
            if quiet_failures:
                print(f"[bot_listener] polling recovered — the last {quiet_failures} were "
                      f"transient network errors")
                quiet_failures = 0
            consecutive_failures = 0
        except Exception as e:
            consecutive_failures += 1
            if isinstance(e, _QUIET_ERRORS):
                quiet_failures += 1
            else:
                print(f"[bot_listener] polling failed({consecutive_failures}): {_safe_err(e)}")
            if consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
                print(
                    f"[bot_listener] consecutive-failure limit exceeded "
                    f"({consecutive_failures}) — exiting to restart: {_safe_err(e)}"
                )
                sys.exit(1)
            time.sleep(5)


if __name__ == "__main__":
    main()
