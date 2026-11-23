"""What every notification channel has to provide.

Messages are composed once, as Telegram-flavoured HTML (`<b>`, `<a href>`),
because that is the richest target and the one whose formatting was tuned
against real messages. Every other channel converts: `to_plain` turns the same
string into something readable in a webhook post, an email body or a terminal.
Composing per-channel variants of each message was the alternative, and it would
have meant every future change to the digest had to be made four times.
"""

import html
import re

_TAG_RE = re.compile(r"<[^>]+>")
_LINK_RE = re.compile(r'<a\s+href="([^"]*)"[^>]*>(.*?)</a>', re.S | re.I)


def to_plain(text):
    """Telegram HTML -> plain text, keeping link targets visible.

    A link's text and its URL both survive, as `text (url)`: in a channel with
    no rich text the URL is the only way to open the posting, and dropping it
    would make the message useless for the one action it exists to prompt.
    """
    text = _LINK_RE.sub(lambda m: f"{m.group(2).strip()} ({m.group(1)})", text or "")
    text = _TAG_RE.sub("", text)
    return html.unescape(text).strip()


def to_markdown(text):
    """Telegram HTML -> markdown, for the local inbox file."""
    text = _LINK_RE.sub(lambda m: f"[{m.group(2).strip()}]({m.group(1)})", text or "")
    text = re.sub(r"</?b>", "**", text, flags=re.I)
    text = re.sub(r"</?i>", "_", text, flags=re.I)
    text = _TAG_RE.sub("", text)
    return html.unescape(text).strip()


class Channel:
    """One place an approval request can go.

    Subclasses override `is_configured`, `status_line`, `_send_text` and, when
    they can carry a file, `send_document`.
    """

    name = "channel"
    # Inline buttons that answer the request in the channel itself. Only
    # Telegram has them; everywhere else `reply_hint` names the CLI command.
    supports_buttons = False

    def is_configured(self):
        raise NotImplementedError

    def status_line(self):
        """One sentence: ready, or exactly what to set to make it ready."""
        raise NotImplementedError

    def reply_hint(self, keyboard=None):
        """How to answer a request that arrived here.

        Appended to messages carrying buttons the channel cannot render. When
        the buttons name their postings — the callbacks are `make:<id>` and
        `skip:<id>` — the hint prints the real ids, so the message is still one
        copy-paste from an answer rather than an instruction to go and look the
        id up.

        No angle brackets: the text is converted out of HTML downstream, and a
        literal `<id>` is indistinguishable from a tag.
        """
        ids = []
        for row in (keyboard or []):
            for _label, callback in row:
                # Callbacks are namespaced (`jw:make:<id>`), so read from the end.
                parts = str(callback).split(":")
                if len(parts) >= 2 and parts[-2] == "make" and parts[-1] not in ids:
                    ids.append(parts[-1])
        if ids:
            lines = ["Answer from the CLI:"]
            for ident in ids[:10]:
                lines.append(f"  python run.py pending --make {ident}"
                             f"   # or --skip {ident}")
            return "\n".join(lines)
        return ("Answer from the CLI:\n"
                "  python run.py inbox                    # what is still pending\n"
                "  python run.py pending --make ID        # draft it\n"
                "  python run.py pending --skip ID        # drop it")

    # ── sending ─────────────────────────────────────────────────────────────

    def send_message(self, text, keyboard=None):
        """Send one message. True when it was actually delivered.

        `keyboard` is [[(label, callback), ...], ...]. A channel that cannot
        render buttons appends `reply_hint()` instead of dropping them silently —
        a request whose buttons vanished reads like a message with no action.
        """
        if keyboard and not self.supports_buttons:
            text = f"{text}\n\n{self.reply_hint(keyboard)}"
        return self._send_text(text, keyboard=keyboard)

    def _send_text(self, text, keyboard=None):
        raise NotImplementedError

    def send_document(self, path, caption=""):
        """Deliver the finished .docx. A channel that cannot send files says so
        in the message rather than failing: the file is on disk either way, and
        its path is what the reader needs."""
        return self.send_message(
            f"{caption}\n\nThe draft is on this machine at:\n{path}" if caption
            else f"The draft is on this machine at:\n{path}")
