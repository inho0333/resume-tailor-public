"""Email, over your own SMTP server.

The channel that needs no app on the phone and no account anywhere new. It has
no buttons either, so requests arrive with the CLI command that answers them.

Environment:
    SMTP_HOST, SMTP_PORT (default 587), SMTP_USER, SMTP_PASSWORD,
    SMTP_FROM (defaults to SMTP_USER), SMTP_TO (defaults to SMTP_FROM)

A password in the environment and not in a file is the same rule the rest of the
project follows: nothing with a credential in it is ever written to disk here.
Use a provider-issued app password, never your account password.
"""

import os
import smtplib
import ssl
from email.message import EmailMessage

from channels import base


def _settings():
    host = os.environ.get("SMTP_HOST", "")
    user = os.environ.get("SMTP_USER", "")
    sender = os.environ.get("SMTP_FROM") or user
    return {
        "host": host,
        "port": int(os.environ.get("SMTP_PORT") or 587),
        "user": user,
        "password": os.environ.get("SMTP_PASSWORD", ""),
        "from": sender,
        "to": os.environ.get("SMTP_TO") or sender,
    }


class Channel(base.Channel):
    name = "email"

    def is_configured(self):
        s = _settings()
        return bool(s["host"] and s["user"] and s["password"] and s["to"])

    def status_line(self):
        if self.is_configured():
            s = _settings()
            return f"ready — digests to {s['to']} (no buttons)"
        return ("set SMTP_HOST, SMTP_USER, SMTP_PASSWORD and SMTP_TO "
                "(an app password, not your account password)")

    def _subject(self, text):
        """The first line of the body, which is the digest's own headline."""
        first = base.to_plain(text).splitlines()
        return (first[0][:120] if first else "Job postings") or "Job postings"

    def _deliver(self, message):
        s = _settings()
        try:
            context = ssl.create_default_context()
            if s["port"] == 465:
                with smtplib.SMTP_SSL(s["host"], s["port"], context=context,
                                      timeout=30) as server:
                    server.login(s["user"], s["password"])
                    server.send_message(message)
            else:
                with smtplib.SMTP(s["host"], s["port"], timeout=30) as server:
                    server.starttls(context=context)
                    server.login(s["user"], s["password"])
                    server.send_message(message)
            return True
        except (smtplib.SMTPException, OSError, ssl.SSLError) as e:
            print(f"[email send failed] {e}")
            return False

    def _send_text(self, text, keyboard=None):
        if not self.is_configured():
            return False
        s = _settings()
        message = EmailMessage()
        message["From"], message["To"] = s["from"], s["to"]
        message["Subject"] = self._subject(text)
        message.set_content(base.to_plain(text))
        return self._deliver(message)

    def send_document(self, path, caption=""):
        if not self.is_configured():
            return False
        if not os.path.exists(path):
            return self.send_message(f"{caption}\n\n(the draft file is missing: {path})")
        s = _settings()
        message = EmailMessage()
        message["From"], message["To"] = s["from"], s["to"]
        message["Subject"] = self._subject(caption or os.path.basename(path))
        message.set_content(base.to_plain(caption) or "The tailored draft is attached.")
        with open(path, "rb") as f:
            message.add_attachment(
                f.read(), maintype="application", filename=os.path.basename(path),
                subtype="vnd.openxmlformats-officedocument.wordprocessingml.document")
        return self._deliver(message)
