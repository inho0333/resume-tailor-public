"""WhatsApp: the interface, and why it is not implemented.

Sending yourself a WhatsApp message is not like posting to a webhook. It needs a
Meta Business account, an app with the WhatsApp product added, a phone number
registered and verified for that app, and — because a business may not open a
conversation with free-form text — a **message template** approved by Meta for
every shape of message you intend to send. A digest whose body changes with the
postings does not fit a fixed template, so the realistic version of this channel
sends a template nudge and makes you open something else to read the content.

That is a real amount of account setup and review latency for a channel that
would deliver less than Telegram does with @BotFather in five minutes. So this
file is the seam and not the feature: `is_configured()` is always False, the
channel never claims to be ready, and `run.py doctor` says why in one line.

If you want it, the shape is: WHATSAPP_TOKEN + WHATSAPP_PHONE_ID +
WHATSAPP_TO, POST to
`https://graph.facebook.com/v21.0/<phone-id>/messages` with a `template`
payload, and `_send_text` mapping the digest onto your approved template's
parameters. `channels/discord.py` is the shortest example of a complete channel
to copy the structure from.
"""

from channels import base

DOCS = "https://developers.facebook.com/docs/whatsapp/cloud-api"


class Channel(base.Channel):
    name = "whatsapp"

    def is_configured(self):
        """Never. A channel that cannot deliver must not report itself ready."""
        return False

    def status_line(self):
        return ("not implemented — needs a Meta Business app and a per-message-shape "
                f"approved template; see channels/whatsapp.py and {DOCS}")

    def _send_text(self, text, keyboard=None):
        return False
