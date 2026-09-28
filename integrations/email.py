"""Outgoing email backends behind one small interface.

``EmailSender.send(to, subject, body) -> message_id`` is all the notification service needs.
Backends, chosen by ``Settings.email_backend``:

* ``outbox`` (default) — writes each message as an RFC 822 ``.eml`` file under
  ``Settings.outbox_path`` (git-ignored). No network; open the files in any mail client.
* ``gmail`` — the real Gmail API sender with OAuth (``integrations/gmail_client.py``).
"""

from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid
from pathlib import Path
from typing import Protocol

from config.settings import get_settings
from services.errors import EmailDeliveryError

FROM_ADDRESS = "Customs Copilot <noreply@customs-copilot.local>"


class EmailSender(Protocol):
    """Anything that can deliver a plain-text email and return its Message-ID."""

    backend: str

    def send(self, to: str, subject: str, body: str) -> str: ...


class OutboxSender:
    """Development backend: "sends" by writing the message to a local ``.eml`` file."""

    backend = "outbox"

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def send(self, to: str, subject: str, body: str) -> str:
        message = EmailMessage()
        message_id = make_msgid(domain="customs-copilot.local")
        message["Message-ID"] = message_id
        message["From"] = FROM_ADDRESS
        message["To"] = to
        message["Subject"] = subject
        message["Date"] = format_datetime(datetime.now(UTC))
        message.set_content(body)
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
            (self.directory / f"{stamp}.eml").write_bytes(bytes(message))
        except OSError as exc:
            raise EmailDeliveryError(f"could not write to outbox {self.directory}: {exc}") from exc
        return message_id


def get_email_sender() -> EmailSender:
    """The sender selected in settings."""
    settings = get_settings()
    if settings.email_backend == "outbox":
        return OutboxSender(settings.outbox_path)
    from integrations.gmail_client import GmailSender  # Google libraries only when needed

    return GmailSender(settings.gmail_credentials_path, settings.gmail_token_path)
