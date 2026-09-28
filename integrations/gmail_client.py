"""Gmail API sender with OAuth 2.0 (architecture §7) — the ``gmail`` email backend.

* Scope ``gmail.send`` only (least privilege: it can send, never read or modify mail).
* One-time consent in a browser (``python -m integrations.gmail_client authorize``) using the
  OAuth desktop client in ``GMAIL_CREDENTIALS_PATH``; the resulting refresh token is cached in
  ``GMAIL_TOKEN_PATH`` — both under the git-ignored ``secrets/`` — and refreshed automatically.
* ``GmailSender.send(to, subject, body)`` never opens a browser: without a usable token it
  raises ``EmailDeliveryError`` telling the operator to run ``authorize``.

Selected with ``EMAIL_BACKEND=gmail`` (``integrations.email.get_email_sender``). CLI::

    .venv\\Scripts\\python.exe -m integrations.gmail_client authorize
    .venv\\Scripts\\python.exe -m integrations.gmail_client send-test [--to someone@example.com]
"""

import argparse
import base64
from email.message import EmailMessage
from email.utils import make_msgid
from pathlib import Path
from typing import Any

from google.auth.exceptions import GoogleAuthError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from config.settings import get_settings
from services.errors import EmailDeliveryError

SCOPES = ["https://www.googleapis.com/auth/gmail.send"]
AUTHORIZE_HINT = "run: .venv\\Scripts\\python.exe -m integrations.gmail_client authorize"


def load_credentials(credentials_path: Path, token_path: Path) -> Credentials:
    """Cached credentials, refreshed if expired. Never interactive."""
    if not token_path.exists():
        raise EmailDeliveryError(
            f"Gmail is not authorized yet (no {token_path.name}); {AUTHORIZE_HINT}"
        )
    try:
        creds: Credentials = Credentials.from_authorized_user_file(  # type: ignore[no-untyped-call]
            str(token_path), SCOPES
        )
        if not creds.valid and creds.expired and creds.refresh_token:
            creds.refresh(Request())  # type: ignore[no-untyped-call]
            token_path.write_text(creds.to_json(), encoding="utf-8")  # type: ignore[no-untyped-call]
    except (GoogleAuthError, ValueError, OSError) as exc:
        raise EmailDeliveryError(f"Gmail credentials unusable ({exc}); {AUTHORIZE_HINT}") from exc
    if not creds.valid:
        raise EmailDeliveryError(f"Gmail token is invalid or revoked; {AUTHORIZE_HINT}")
    return creds


def authorize(credentials_path: Path, token_path: Path) -> Credentials:
    """One-time browser consent; caches the refresh token for unattended sends."""
    if not credentials_path.exists():
        raise EmailDeliveryError(
            f"OAuth client file not found at {credentials_path} — download it from Google Cloud "
            "Console (see docs/setup_gmail.md)"
        )
    flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), SCOPES)
    creds: Credentials = flow.run_local_server(port=0)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json(), encoding="utf-8")  # type: ignore[no-untyped-call]
    return creds


def build_message(to: str, subject: str, body: str) -> tuple[str, dict[str, str]]:
    """(RFC Message-ID, Gmail API request body) for a plain-text email. ``From`` is omitted:
    Gmail sets it to the authorized account."""
    message = EmailMessage()
    message_id = make_msgid(domain="customs-copilot.local")
    message["Message-ID"] = message_id
    message["To"] = to
    message["Subject"] = subject
    message.set_content(body)
    raw = base64.urlsafe_b64encode(bytes(message)).decode("ascii")
    return message_id, {"raw": raw}


class GmailSender:
    """Sends through the Gmail API as the authorized account (``integrations.email`` backend)."""

    backend = "gmail"

    def __init__(self, credentials_path: Path, token_path: Path) -> None:
        self.credentials_path = credentials_path
        self.token_path = token_path

    def _service(self) -> Any:
        creds = load_credentials(self.credentials_path, self.token_path)
        return build("gmail", "v1", credentials=creds, cache_discovery=False)

    def send(self, to: str, subject: str, body: str) -> str:
        message_id, request_body = build_message(to, subject, body)
        try:
            self._service().users().messages().send(userId="me", body=request_body).execute()
        except HttpError as exc:
            raise EmailDeliveryError(f"Gmail API refused the message: {exc}") from exc
        except (GoogleAuthError, OSError) as exc:
            raise EmailDeliveryError(f"Gmail API unreachable: {exc}") from exc
        return message_id


def main(argv: list[str] | None = None) -> None:
    settings = get_settings()
    parser = argparse.ArgumentParser(prog="python -m integrations.gmail_client")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("authorize", help="one-time browser consent; caches the token")
    test = commands.add_parser("send-test", help="send a test email via the Gmail API")
    test.add_argument("--to", default=settings.reviewer_email)
    args = parser.parse_args(argv)

    if args.command == "authorize":
        authorize(settings.gmail_credentials_path, settings.gmail_token_path)
        print(f"Authorized. Token cached at {settings.gmail_token_path}")
        return
    if not args.to:
        parser.error("no --to given and REVIEWER_EMAIL is not set")
    sender = GmailSender(settings.gmail_credentials_path, settings.gmail_token_path)
    message_id = sender.send(
        args.to,
        "[Customs Copilot] Gmail test",
        "This is a test from the Port & Customs Clearance Copilot. Gmail sending works.",
    )
    print(f"Sent to {args.to} (Message-ID {message_id})")


if __name__ == "__main__":
    main()
