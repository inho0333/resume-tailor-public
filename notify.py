"""Telegram notifications — thin layer over vendor/telegram.py.

vendor/telegram.py has the four levels (silent/info/alert/critical) and plain
sendMessage. This module adds document upload (sendDocument) and the per-posting
Report object. With TG_BOT_TOKEN/TG_CHAT_ID unset every send is skipped and the
same text goes to the console, so nothing here is required to run the tool.

## One notification per posting

Each stage used to send its own message while a posting was processed — eligibility
caution, work-authorisation caution, company research, docx, remaining gaps. Add the
"new posting" alert from an upstream job watcher and a single posting could fire six
messages within a few seconds. Measured once: only 5 seconds separated the company
research message from the gap message. On a phone, fragments about the same posting
drift apart and lose their context.

Now `Report` collects each stage's result and sends **once at the end**, in
`Report.send()` — one docx attachment whose caption carries the whole summary. Only
the "new posting found" alert still has to be immediate, and an upstream job watcher
(not part of this repo) sends that one line first: the drafting pipeline takes a
measured 2-5 minutes, too long to hold the first alert back.

If the pipeline breaks partway through, **whatever was collected still goes out** —
run.py's except block fills in only `error` on the same Report and sends it. Nothing
here waits for every stage to succeed.
"""

import json
import os

import requests

from vendor import telegram

# The four levels (silent/info/alert/critical) live in vendor/telegram.py. With no
# token they all fall back to console output and the pipeline keeps running.
silent = telegram.silent
info = telegram.info
alert = telegram.alert
critical = telegram.critical

_API_BASE = telegram.API_BASE
_CHAT_ID = telegram.CHAT_ID

# Telegram's sendDocument caption limit. Going over returns a 400 and **the document
# itself never arrives**, so when the text overflows we drop the lowest-priority
# blocks first (see _render). Budgeted 24 characters short of the real limit.
CAPTION_LIMIT = 1000

# A recruiter score below this gets a ⚠️ in the report. Read from config to avoid a
# second definition, with a default for callers that import this module without config.
try:
    import config as _config

    _RECRUITER_WARN = _config.RECRUITER_SCORE_WARN
except Exception:
    _RECRUITER_WARN = 7

_VERDICT_LABEL = {
    "advance": "would advance",
    "borderline": "borderline",
    "reject": "would be cut",
}


def send_message(text, keyboard=None):
    """Send one text message. `keyboard` is an inline_keyboard laid out **row by row**
    ([[(label, callback), ...], ...]) — `info(buttons=...)` in vendor/telegram.py wraps
    buttons three to a row, which splits a posting's ✅/⏭️ pair across rows and leaves
    you unable to tell which posting a button belongs to (a digest message carries up
    to 10 postings x 2 buttons).

    `send` in vendor/telegram.py swallows every failure and times out after 5 seconds.
    For a digest, "did this message actually arrive" is the whole day's output, so this
    function reports whether it did.
    Returns: whether the send succeeded."""
    if not telegram.is_configured():
        silent(f"[telegram not configured] message not sent:\n{text}")
        return False
    payload = {"chat_id": _CHAT_ID, "text": text, "parse_mode": "HTML",
               "disable_web_page_preview": True}
    if keyboard:
        payload["reply_markup"] = json.dumps({
            "inline_keyboard": [[{"text": label, "callback_data": cb} for label, cb in row]
                                for row in keyboard]
        })
    try:
        resp = requests.post(f"{_API_BASE}/sendMessage", json=payload, timeout=20)
        resp.raise_for_status()
        return True
    except requests.RequestException as e:
        # Log the response body too — on a 400 (HTML parsing, caption length) the log is
        # the only way to track down which character caused it.
        detail = getattr(getattr(e, "response", None), "text", "")[:300]
        silent(f"[telegram send failed] {e} {detail}\n{text[:500]}")
        return False


def send_document(path, caption=""):
    """Send the docx draft over Telegram — review is always done by a human, on a phone.
    This is the closest thing in the system to "submitting an application", but all it
    really does is send a file to you (no submission API is ever called)."""
    if not telegram.is_configured():
        silent(f"[telegram not configured] document not sent: {path}")
        return False
    if not os.path.exists(path):
        alert(f"document upload failed — no such file: {path}")
        return False
    try:
        with open(path, "rb") as f:
            resp = requests.post(
                f"{_API_BASE}/sendDocument",
                data={"chat_id": _CHAT_ID, "caption": caption, "parse_mode": "HTML"},
                files={"document": f},
                timeout=30,
            )
        resp.raise_for_status()
        return True
    except requests.RequestException as e:
        alert(f"document upload failed: {e}")
        return False


# ── One report per posting ────────────────────────────────────────────────────

def _esc(s):
    """Escape for Telegram's HTML parse_mode. Skip it and strings that are common in
    posting text, like `R&D` or `<$1M`, return 400 Bad Request and **the whole message
    is lost**. Quotes are escaped too because URLs go into href attributes (in body
    text &quot; renders back as a plain ", so it is harmless there)."""
    return (str(s or "").replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _clip(s, n):
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


# Public aliases for callers that build a message directly, without a Report — such as
# run.py's `_send_confirmation_request` (the confirm-then-generate flow). `_esc`/`_clip`
# themselves started as private helpers used only inside this file.
esc = _esc
clip = _clip


class Report:
    """Accumulates the results for one posting. Each stage calls a set_*, and at the
    end there is exactly one send(), whether the run succeeded or failed."""

    def __init__(self, company, role, url=None):
        self.company = company or "Unknown"
        self.role = role or "Unknown"
        self.url = url
        self.eligibility = None   # {"verdict", "reasoning", "quote"}
        self.visa = None          # {"note"}  — filled in only on a caution
        self.sponsorship = None   # {"quote"} — only when the "no sponsorship" wording hit
        self.research = None      # {"notes"} — filled in only when notable
        self.docx_path = None
        self.bullets = {}         # {section_name: count}
        self.minimal_edit = None  # {"swaps": int, "edits": int} — minimal-mode tailoring
        self.pagefit = None       # {"level": "ok"|"tight"|"over", "text": str}
        self.gaps = []
        self.error = None
        self.recruiter = None     # {"score", "verdict", "concerns": [...], "applied": int}
        self.keyboard = None      # button rows, text path only (e.g. "retry" on a failure)

    # Stage by stage ─────────────────────────────────────
    def set_eligibility(self, eligibility):
        self.eligibility = {
            "verdict": eligibility.get("degree_eligible", "unclear"),
            "reasoning": (eligibility.get("reasoning") or "").strip(),
            "quote": (eligibility.get("quoted_requirement") or "").strip(),
        }
        if eligibility.get("visa_caution"):
            self.visa = {"note": (eligibility.get("visa_note") or "").strip()}
        if eligibility.get("sponsorship_excluded"):
            self.sponsorship = {"quote": (eligibility.get("sponsorship_quote") or "").strip()}

    def set_research(self, research):
        if research and research.get("notable") and research.get("notes"):
            self.research = {"notes": research["notes"].strip()}

    def set_draft(self, docx_path, applied):
        self.docx_path = docx_path
        self.bullets = {name: len(ids) for name, ids in (applied or {}).items()}

    def set_minimal_edit(self, applied):
        """How much minimal mode actually tailored — swap and edit counts in the report.

        Measured once: a second run finished with zero swaps and the report gave no way
        to tell ("13 bullets" prints the same for an untouched résumé). With no swaps at
        all, a ⚠️ goes on the line so you notice immediately."""
        if not applied:
            return
        self.minimal_edit = {"swaps": len(applied.get("swaps") or []),
                             "edits": len(applied.get("edits") or [])}

    def set_pagefit(self, level, text):
        self.pagefit = {"level": level, "text": text}

    def set_gaps(self, gaps):
        self.gaps = [g for g in (gaps or []) if (g or {}).get("item")]

    def set_recruiter(self, review):
        """Put the recruiter-perspective review (recruiter_review.review's return value)
        into the report. On a phone the first thing you want to know is what a real
        recruiter would score this draft."""
        if not review:
            return
        self.recruiter = {
            "score": review.get("screening_score"),
            "verdict": review.get("verdict"),
            "concerns": [c for c in (review.get("top_concerns") or []) if c][:2],
            "applied": review.get("applied_count") or 0,
        }

    def set_error(self, message):
        self.error = _clip(message, 300)

    def set_keyboard(self, keyboard):
        self.keyboard = keyboard

    # Rendering ──────────────────────────────────────────
    @property
    def _has_caution(self):
        if self.eligibility and self.eligibility["verdict"] != "yes":
            return True
        if self.visa or self.sponsorship:
            return True
        if self.pagefit and self.pagefit["level"] == "over":
            return True
        return False

    def _blocks(self):
        """List of (priority, text). Higher priority is dropped first to fit the caption."""
        if self.error:
            glyph, headline = "❌", "Résumé draft failed"
        elif self._has_caution:
            glyph, headline = "⚠️", "Résumé draft ready (check this)"
        else:
            glyph, headline = "✅", "Résumé draft ready"

        # The header is priority 0 and is never dropped, so an unusually long role title
        # is clipped here to keep it under the limit (otherwise render's last resort,
        # raw truncation, could cut through the middle of an HTML tag).
        head = (f"<b>{glyph} {headline}</b>\n"
                f"{_esc(_clip(self.company, 80))} · {_esc(_clip(self.role, 120))}")
        blocks = [(0, head)]

        # The verdicts, as lines you scan at a glance. Phones use a proportional font, so
        # whitespace alignment falls apart: only "• label: value" is used.
        checks = []
        if self.eligibility:
            v = self.eligibility["verdict"]
            if v == "yes" and not self.visa and not self.sponsorship:
                # Nothing wrong with either, so they share a line — this is the common
                # case and there is no reason to spend two lines on it.
                checks.append("• Eligibility: degree requirement met, "
                              "no work-authorisation flags")
            else:
                if v == "yes":
                    checks.append("• Degree: the posting's degree requirement is met")
                elif v == "no":
                    checks.append("• Degree: ⚠️ the posting may require a degree "
                                  "you do not have")
                else:
                    checks.append("• Degree: the posting says too little to judge — check it")
                # The sponsorship exclusion sits above the work-authorisation caution: it
                # is the harder blocker, since it rules out the candidate outright rather
                # than flagging a requirement to verify.
                if self.sponsorship:
                    checks.append("• Sponsorship: ⚠️ this posting excludes candidates "
                                  "who will need sponsorship")
                elif self.visa:
                    checks.append("• Work authorisation: ⚠️ the posting requires citizenship "
                                  "or a clearance")
                else:
                    checks.append("• Work authorisation: nothing in the posting rules you out")
        if self.pagefit:
            mark = {"ok": "", "tight": "⚠️ ", "over": "⚠️ "}[self.pagefit["level"]]
            checks.append(f"• Length: {mark}{_esc(self.pagefit['text'])}")
        if self.bullets:
            breakdown = " · ".join(f"{_esc(n)} {c}" for n, c in self.bullets.items())
            checks.append(f"• Content: {sum(self.bullets.values())} bullets ({breakdown})")
        if self.minimal_edit:
            swaps, edits = self.minimal_edit["swaps"], self.minimal_edit["edits"]
            mark = "" if swaps else "⚠️ "
            tail = "" if swaps else " (identical to the approved résumé)"
            checks.append(f"• Tailoring: {mark}{swaps} swapped · {edits} reworded{tail}")
        if self.recruiter and self.recruiter["score"] is not None:
            score = self.recruiter["score"]
            mark = "" if score >= _RECRUITER_WARN else "⚠️ "
            verdict = _VERDICT_LABEL.get(self.recruiter["verdict"], self.recruiter["verdict"] or "")
            applied = (f", {self.recruiter['applied']} lines rewritten"
                       if self.recruiter["applied"] else "")
            checks.append(f"• Recruiter review: {mark}{score}/10 {_esc(verdict)}{applied}")
        if checks:
            blocks.append((1, "\n".join(checks)))

        if self.error:
            blocks.append((0, f"<b>What failed</b>\n{_esc(self.error)}"))

        # What the recruiter flagged goes above the gaps: a gap is experience the database
        # does not have, so there is nothing to do about it right now, while these are
        # things you can still fix in the sentences you have.
        if self.recruiter and self.recruiter["concerns"]:
            lines = [f"• {_esc(_clip(c, 120))}" for c in self.recruiter["concerns"]]
            blocks.append((2, "<b>What the recruiter caught</b>\n" + "\n".join(lines)))

        if self.gaps:
            shown = self.gaps[:4]
            lines = [f"• {_esc(_clip(g['item'], 90))}" for g in shown]
            if len(self.gaps) > len(shown):
                lines.append(f"• and {len(self.gaps) - len(shown)} more")
            blocks.append((3, "<b>Requirements the résumé could not cover</b>\n"
                           + "\n".join(lines)))

        # The posting's own wording behind an "ineligible" verdict. You need it to decide
        # for yourself whether it really rules you out, but it is long, so it is the first
        # block to be dropped.
        if self.eligibility and self.eligibility["verdict"] == "no":
            detail = []
            if self.eligibility["reasoning"]:
                detail.append(_esc(_clip(self.eligibility["reasoning"], 200)))
            if self.eligibility["quote"]:
                detail.append(f"<i>“{_esc(_clip(self.eligibility['quote'], 180))}”</i>")
            if detail:
                blocks.append((5, "<b>Why the degree requirement failed</b>\n" + "\n".join(detail)))
        if self.sponsorship and self.sponsorship["quote"]:
            blocks.append((4, "<b>Sponsorship wording (from the posting)</b>\n"
                           + _esc(_clip(self.sponsorship["quote"], 250))))
        if self.visa and self.visa["note"]:
            blocks.append((4, "<b>Work authorisation wording</b>\n"
                           + _esc(_clip(self.visa["note"], 200))))

        if self.research:
            blocks.append((6, "<b>Company news</b>\n" + _esc(_clip(self.research["notes"], 260))))

        if self.url:
            blocks.append((0, f'<a href="{_esc(self.url)}">Open the posting</a>'))

        return blocks

    def render(self, limit=CAPTION_LIMIT):
        blocks = self._blocks()
        while True:
            text = "\n\n".join(t for _, t in blocks)
            if len(text) <= limit:
                return text
            droppable = [i for i, (p, _) in enumerate(blocks) if p > 0]
            if not droppable:
                return text[: limit - 1] + "…"
            # Drop the block with the highest priority number (= the least important).
            blocks.pop(max(droppable, key=lambda i: blocks[i][0]))

    def send(self):
        """One document (caption = the full report) when there is a docx, otherwise one
        text message. Either way, exactly one message."""
        text = self.render()
        if self.docx_path and os.path.exists(self.docx_path):
            if send_document(self.docx_path, caption=text):
                # Mirror what was sent to the console (= logs/poll.log). The caption goes
                # out through sendDocument, bypassing silent/info, so without this the log
                # would not record what actually reached the phone.
                silent(f"[report sent]\n{text}")
                return
            # A failed document upload must not take the analysis down with it.
            info(text + f"\n\n(document upload failed — local file: {_esc(self.docx_path)})")
            return
        # Deliberately not alert(): the shared notify prefixes "!!", and the report already
        # shows severity with its ❌/⚠️ glyph, so it would both duplicate that and feed the
        # impression that nothing but errors ever arrives.
        if not send_message(text, keyboard=self.keyboard):
            # On a failed send, try once more through the shared notify — the buttons are
            # lost, but the content still gets through.
            info(text)
