"""A file lock that keeps draft generation to **one at a time, globally**.

## Why this is needed (measured from the logs)

`confirm_handler.handle_make` starts a thread per Telegram callback, and the project
had no lock anywhere (`grep -rn "Lock"` → 0 hits). But the user really does press
buttons back to back — two postings **generated at once, 17 seconds apart** — and each
one takes 3 to 5 minutes, so the two pipelines overlapped for nearly their whole run.
That was survivable at the time, but once `pagefit.py` (the Word osascript measurement)
arrived, overlap became a real incident:

  - There is only **one** Word instance. `measure()` opens the document and walks the
    cursor with `select` to read line boundaries, and the selection is application-wide
    state. Two interleaved measurements move each other's cursor and the line counts go
    wrong, and one side's `close ... saving no` can close the document the other side is
    still measuring.
  - The subscription CLI backend is also called 2-3 times per posting (prompts up to
    53KB each), and running two at once burns the session budget twice as fast. Of 10
    measured failures, 8 were an exit 1 with empty stdout/stderr, and that pattern is
    indistinguishable from hitting the session limit (see the vendor/claude_cli.py
    comments).

So "one draft generation at a time on this machine" is enforced with a file lock. Why a
file lock and not a thread lock: generation starts both from the always-on daemon
(a bot_listener thread) and from the CLI a person runs by hand (`run.py tailor`), and
those are two different processes.

The waiting side waits (20 minutes by default) — refusing would make the button the
user pressed simply disappear.
"""

import datetime
import errno
import fcntl
import os
import time

import config

LOCK_PATH = os.path.join(config.STATE_DIR, "generation.lock")

# How long to wait for the generation ahead to finish. One takes 3 to 5 minutes, longer
# with pagefit fixes on top, so this is generous enough to absorb two or three in line.
# Past it, give up and hand the retry back to the user.
DEFAULT_TIMEOUT_SEC = 1200
_POLL_SEC = 2


class LockBusy(RuntimeError):
    """The lock could not be taken within the time limit — meaning the one ahead is
    taking abnormally long."""


class GenerationLock:
    """Used as `with GenerationLock(on_wait=...)`. On acquiring it, the owner's details
    are written into the file, so the waiting side can log "who is holding it, and why"."""

    def __init__(self, label="", timeout_sec=DEFAULT_TIMEOUT_SEC, on_wait=None):
        self.label = label
        self.timeout_sec = timeout_sec
        self.on_wait = on_wait
        self._fd = None

    def _holder(self):
        try:
            with open(LOCK_PATH, "r", encoding="utf-8") as f:
                return f.read().strip()
        except OSError:
            return ""

    def __enter__(self):
        os.makedirs(config.STATE_DIR, exist_ok=True)
        # Open with O_CREAT and take an flock. The file is never deleted (deleting it
        # creates a race), so the contents are overwritten each time — the lock file
        # itself doubles as the "current owner" noticeboard.
        self._fd = os.open(LOCK_PATH, os.O_RDWR | os.O_CREAT, 0o600)
        deadline = time.time() + self.timeout_sec
        notified = False
        while True:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as e:
                if e.errno not in (errno.EAGAIN, errno.EACCES):
                    os.close(self._fd)
                    self._fd = None
                    raise
                if not notified and self.on_wait:
                    self.on_wait(self._holder())
                    notified = True
                if time.time() >= deadline:
                    os.close(self._fd)
                    self._fd = None
                    raise LockBusy(
                        f"Another draft generation has been running for over "
                        f"{self.timeout_sec}s "
                        f"(current holder: {self._holder() or 'unknown'})"
                    )
                time.sleep(_POLL_SEC)
        os.ftruncate(self._fd, 0)
        os.write(
            self._fd,
            f"pid={os.getpid()} {self.label} "
            f"since={datetime.datetime.now().isoformat(timespec='seconds')}\n".encode(),
        )
        os.fsync(self._fd)
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._fd is not None:
            try:
                os.ftruncate(self._fd, 0)
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
                self._fd = None
        return False
