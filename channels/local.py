"""The default channel: a markdown inbox file and the terminal.

It exists so that the pipeline has somewhere to put an approval request on a
machine with no credentials at all. Nothing about this project's core work —
reading a posting, judging fit, editing the approved résumé — needs an account
anywhere, and the first run should not be blocked on creating one.

Requests are appended to `state/inbox.md`, newest last, each with the command
that answers it. `run.py inbox` prints what is still pending.
"""

import datetime
import os

import config
from channels import base

PATH = os.path.join(config.STATE_DIR, "inbox.md")

_HEADER = """# Inbox

Approval requests, newest last. Each one names the command that answers it;
`python run.py inbox` prints only what is still pending.

This file is appended to, never rewritten, and it is gitignored along with the
rest of `state/`.
"""


class Channel(base.Channel):
    name = "local"

    def is_configured(self):
        """Always. That is the point of it."""
        return True

    def status_line(self):
        return f"ready — requests are appended to {PATH}"

    def _send_text(self, text, keyboard=None):
        body = base.to_markdown(text)
        stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        try:
            os.makedirs(config.STATE_DIR, exist_ok=True)
            new = not os.path.exists(PATH)
            with open(PATH, "a", encoding="utf-8") as f:
                if new:
                    f.write(_HEADER)
                f.write(f"\n\n---\n\n## {stamp}\n\n{body}\n")
            os.chmod(PATH, 0o600)
        except OSError as e:
            # Console output is the fallback for the fallback. A notification
            # that cannot be written must not take down the run that made it.
            print(f"[inbox write failed: {e}]")
            print(body)
            return False
        print(body)
        print(f"\n[written to {PATH}]")
        return True
