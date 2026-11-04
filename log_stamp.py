"""Prefixes every line of stdout/stderr with a timestamp.

Why this is needed: launchd sends stdout straight to `logs/bot_listener.log`, but what the
code writes is `print()`, so **almost nothing was timestamped** (137 of 170 lines). notify
does prefix `[HH:MM:SS]`, but with no date — so in a file that accumulates for days without
rotation, even those lines did not say which day they belonged to.
This is the same implementation as the equivalent file in a sibling project of mine; it is
duplicated rather than shared because they are separate repos.

Rather than fixing every print call site, the stream is wrapped one layer deep — that way
lines printed by libraries are caught too, and there is no rule to remember when adding a new
print. The date is included because the log accumulates for days without rotation, so
hour:minute:second alone does not say which day it was.
"""

import sys
from datetime import datetime


class _Stamped:
    def __init__(self, stream):
        self._stream = stream
        self._at_line_start = True

    def write(self, data):
        if not data:
            return 0
        stamp = datetime.now().strftime("%m-%d %H:%M:%S")
        out = []
        for ch in data.splitlines(keepends=True):
            if self._at_line_start and ch.strip():
                out.append(f"[{stamp}] ")
            out.append(ch)
            self._at_line_start = ch.endswith("\n")
        return self._stream.write("".join(out))

    def flush(self):
        return self._stream.flush()

    def isatty(self):
        return self._stream.isatty()

    def fileno(self):
        return self._stream.fileno()

    def __getattr__(self, name):
        return getattr(self._stream, name)


def install():
    """Call once at process start. An already-wrapped stream is not wrapped again."""
    if not isinstance(sys.stdout, _Stamped):
        sys.stdout = _Stamped(sys.stdout)
    if not isinstance(sys.stderr, _Stamped):
        sys.stderr = _Stamped(sys.stderr)
