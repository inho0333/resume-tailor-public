"""The digest: approval requests **batched into one message, twice a day**.

## Why (measured)

Sending one approval message per posting, immediately, recreated the notification
flood: 15 arrived in a single day, 11 of them inside two and a half hours. The rule an
upstream job watcher had established — one posting, one message (see the comment at the
top of notify.py) — was back to square one at the approval gate. The result was 13
answers out of 87 requests (15%), with 43 expiring unanswered after 7 days.

So requests pile up in `confirm_state` instead (free — the model is not called once
until a button is pressed), and `bot_listener` sends **a single message, sorted best
fit first**, at the hours in `config.DIGEST_HOURS`. Each posting keeps its own ✅/⏭️
pair on its own row, so you can read one screen and tap straight down it.

## Design decisions

- **A weak fit or a failed eligibility check is never asked about with ✅.** It is only
  listed at the bottom as "N not asked about", with a single "draft it anyway" button
  (a person has to be able to overturn a wrong verdict).
- At most `config.DIGEST_MAX_ITEMS` postings get buttons. The rest roll over to the
  next digest, which respects both Telegram's 4096-character body limit and how much
  a person can actually decide on in one screen.
- A request already sent is stamped with `digest_sent_at` and never sent again. There
  seemed to be no need for a second reminder before the 7-day TTL runs out: a posting
  worth a reminder gets tapped the same day.
- Nothing is sent when there is nothing to send (no reason to get a "0 pending"
  message on a quiet day).
"""

import datetime

import config
import confirm_state
import fit_gate
import notify

_TIER_GLYPH = {"high": "🟢", "mid": "🟡", "low": "⚪"}


def _unsent(records):
    return {sid: r for sid, r in records.items()
            if not r.get("digest_sent_at") and not confirm_state.is_expired(r)}


def _sort_key(item):
    """Best fit first, then oldest first. Oldest goes first because its TTL expires first."""
    sid, r = item
    return (fit_gate.sort_key(r.get("prefit")), r.get("created_at") or "")


# Telegram's sendMessage body limit is 4096 characters. Over that it returns a 400 and
# **the whole digest fails to arrive** — measured: 24 pending postings rendered in full
# detail came to 4,259 characters (the day's only output, gone). As in notify.Report,
# going over the limit steps the detail level down.
TEXT_LIMIT = 3900  # 196 characters of slack (emoji and link lengths vary)


def _item_lines(idx, record, detail):
    """The display lines for one posting. detail: "full" | "compact"."""
    prefit = record.get("prefit") or {}
    tier = prefit.get("tier", "mid")
    glyph = _TIER_GLYPH.get(tier, "🟡")
    head = (f"{idx}. {glyph} <b>{notify.esc(notify.clip(record['company'], 40))}</b> · "
            f"{notify.esc(notify.clip(record['role'], 60))}")
    if detail == "compact":
        # Fit label and link only — the minimum needed to tell the questions apart.
        tail = f"   {fit_gate.TIER_LABEL.get(tier, 'possible fit')}"
        if record.get("url"):
            tail += f' · <a href="{notify.esc(record["url"])}">posting</a>'
        return head + "\n" + tail

    sub = [f"   {fit_gate.TIER_LABEL.get(tier, 'possible fit')}"]
    if prefit.get("reason"):
        sub[0] += f" — {notify.esc(notify.clip(prefit['reason'], 90))}"
    if (record.get("eligibility") or {}).get("sponsorship_excluded"):
        sub.append("   ⚠️ excludes candidates who will need sponsorship")
    if (record.get("eligibility") or {}).get("visa_caution"):
        sub.append("   ⚠️ work authorisation: citizenship or clearance required")
    if prefit.get("unmet"):
        sub.append("   not covered: " + notify.esc(notify.clip(" / ".join(prefit["unmet"]), 90)))
    if record.get("url"):
        sub.append(f'   <a href="{notify.esc(record["url"])}">open the posting</a>')
    return head + "\n" + "\n".join(sub)


def _render(askable, silent_items, detail, show_silent_detail):
    lines = [f"<b>🤔 Draft these? ({len(askable)})</b>"]
    for idx, (_sid, r) in enumerate(askable, 1):
        lines.append(_item_lines(idx, r, detail))

    if silent_items:
        header = (f"<b>🔕 {len(silent_items)} not asked about</b> (ineligible or a weak fit "
                  f"— ignore them and they clear themselves after "
                  f"{config.CONFIRM_TTL_DAYS} days)")
        if not show_silent_detail:
            names = ", ".join(notify.esc(notify.clip(r["company"], 20))
                              for _sid, r in silent_items[:8])
            lines.append(f"{header}\n{names}" + (" …" if len(silent_items) > 8 else ""))
        else:
            body = []
            for idx, (_sid, r) in enumerate(silent_items, len(askable) + 1):
                prefit = r.get("prefit") or {}
                reason = prefit.get("reason") or (r.get("eligibility") or {}).get("reasoning") or ""
                body.append(f"{idx}. {notify.esc(notify.clip(r['company'], 32))} · "
                            f"{notify.esc(notify.clip(r['role'], 44))}\n"
                            f"   {notify.esc(notify.clip(reason, 80))}")
            lines.append(header + "\n" + "\n".join(body))
    return "\n\n".join(lines)


def build(records):
    """Build the digest body and keyboard. Returns: (text, keyboard, asked_ids, listed_ids),
    or (None, None, [], []) when there is nothing to send."""
    pending = sorted(_unsent(records).items(), key=_sort_key)
    if not pending:
        return None, None, [], []

    askable, silent_items = [], []
    for sid, r in pending:
        # Whether to ask is decided in one place, fit_gate (including spotting a
        # contradiction that overturns an eligibility verdict of 'no').
        (askable if fit_gate.should_ask_record(r) else silent_items).append((sid, r))

    askable = askable[:config.DIGEST_MAX_ITEMS]

    # Over the limit, (1) collapse the detail on the ones not asked about, then
    # (2) compact the asked items, then (3) drop asked items. Dropping comes last
    # because a dropped record stays in state and comes back in the next digest, so
    # no information is lost.
    for show_silent_detail in (True, False):
        for detail in ("full", "compact"):
            text = _render(askable, silent_items, detail, show_silent_detail)
            if len(text) <= TEXT_LIMIT:
                break
        else:
            continue
        break
    while len(text) > TEXT_LIMIT and len(askable) > 1:
        askable = askable[:-1]
        text = _render(askable, silent_items, "compact", False)

    keyboard = [[(f"✅ {idx}", f"jw:make:{sid}"), (f"⏭️ {idx}", f"jw:skip:{sid}")]
                for idx, (sid, _r) in enumerate(askable, 1)]
    asked_ids = [sid for sid, _r in askable]

    # Buttons for the "not asked about" items only fill the button slots left over
    # (the list itself always shows all of them).
    listed_ids = [sid for sid, _r in silent_items]
    for offset, (sid, _r) in enumerate(silent_items):
        if len(keyboard) >= config.DIGEST_MAX_ITEMS:
            break
        keyboard.append([(f"Draft anyway {len(askable) + offset + 1}", f"jw:make:{sid}")])

    return text, keyboard, asked_ids, listed_ids


def send_if_due(now=None, force=False, log=print):
    """Send the digest if it is due. `bot_listener`'s polling loop calls this periodically.

    The last send time is kept in state so the same hour never sends twice.
    Returns: how many postings went out (0 means nothing was sent).
    """
    now = now or datetime.datetime.now()
    if not config.DIGEST_HOURS and not force:
        return 0
    if not force:
        last = confirm_state.get_meta("digest_last_sent")
        if now.hour not in config.DIGEST_HOURS:
            return 0
        if last and last[:13] == now.isoformat()[:13]:
            return 0  # already sent this hour (deduplicated at hour granularity).

    records = confirm_state.all_records()
    text, keyboard, asked_ids, listed_ids = build(records)
    if not text:
        if force:
            log("[digest] nothing pending to send")
        return 0

    if not notify.send_message(text, keyboard=keyboard):
        # Nothing went out, so **digest_sent_at is not stamped** — the next cycle retries.
        log("[digest] send failed, retrying on the next cycle")
        return 0

    stamp = now.isoformat(timespec="seconds")
    for sid in asked_ids + listed_ids:
        confirm_state.update(sid, digest_sent_at=stamp)
    confirm_state.set_meta("digest_last_sent", stamp)
    log(f"[digest] sent — {len(asked_ids)} asked about, {len(listed_ids)} listed")
    return len(asked_ids) + len(listed_ids)
