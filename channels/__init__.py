"""Where an approval request goes, and how you answer it.

The pipeline used to talk to Telegram directly. Telegram is the best of these
— it is the only one with inline buttons, so a posting can be approved with one
tap on a phone — but it is also a bot token, a chat id and a long-running
listener, and requiring all three before anything works put a wall in front of
the first run. **No channel is required.** With nothing configured, requests
land in `state/inbox.md` and are answered with `run.py pending --make <id>`.

Each channel is outbound plus a sentence saying how to answer it. Only
Telegram's answer comes back through the channel itself (bot_listener.py);
everywhere else the answer is a CLI command, which is why `reply_hint` is part
of the interface rather than an afterthought.

Priority, as `run.py setup` offers them:

  1. telegram — inline ✅/⏭️ buttons, a phone-shaped review. ~5 minutes to set
     up with @BotFather.
  2. discord  — a webhook URL and nothing else, so it is the quickest to set up,
     but a webhook cannot carry buttons: you answer from the CLI.
  3. email    — works on any phone with no app, via your own SMTP credentials.
     No buttons either.
  4. local    — always available, zero setup, and the default. A markdown inbox
     plus the terminal.
  5. whatsapp — interface only. Sending to yourself needs a Meta Business app,
     a reviewed number and a template per message shape; that is a heavier
     commitment than the rest of this project asks for, so it is not
     implemented. See `channels/whatsapp.py`.
"""

from channels import base, discord, email, local, telegram, whatsapp

# Registration order is display order in `run.py setup` and `run.py doctor`.
CHANNELS = {
    "telegram": telegram.Channel,
    "discord": discord.Channel,
    "email": email.Channel,
    "local": local.Channel,
    "whatsapp": whatsapp.Channel,
}

__all__ = ["CHANNELS", "base", "telegram", "discord", "email", "local", "whatsapp",
           "enabled", "describe"]


def _instances(names):
    out = []
    for name in names:
        factory = CHANNELS.get(name)
        if factory is None:
            print(f"  [warn] unknown notification channel {name!r} — "
                  f"known: {', '.join(CHANNELS)}")
            continue
        out.append(factory())
    return out


def enabled(names=None):
    """The channels to send through, in order.

    names: from user_config.yaml. ["auto"] — the default — means the local inbox
    plus every channel whose credentials happen to be present, which is what
    keeps an existing Telegram setup working with no configuration at all.
    """
    names = list(names or ["auto"])
    if "auto" in names:
        resolved = [local.Channel()]
        for name, factory in CHANNELS.items():
            if name == "local":
                continue
            channel = factory()
            if channel.is_configured():
                resolved.append(channel)
        return resolved
    return [c for c in _instances(names) if c.is_configured() or c.name == "local"]


def describe(names=None):
    """One line per known channel, for `run.py doctor`."""
    selected = {c.name for c in enabled(names)}
    rows = []
    for name, factory in CHANNELS.items():
        channel = factory()
        rows.append({
            "name": name,
            "configured": channel.is_configured(),
            "selected": name in selected,
            "buttons": channel.supports_buttons,
            "detail": channel.status_line(),
        })
    return rows
