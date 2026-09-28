"""Phase 6 — the Gmail sender, with the Google API and OAuth mocked (no network, no browser)."""

import base64
import json
from email import message_from_bytes
from pathlib import Path
from typing import Any

import httplib2
import pytest
from google.oauth2.credentials import Credentials
from googleapiclient.errors import HttpError

from integrations import gmail_client
from services.errors import EmailDeliveryError


class FakeGmail:
    """Stands in for ``build("gmail", "v1", ...)``: records what would be sent."""

    def __init__(self, error: Exception | None = None) -> None:
        self.sent: list[dict[str, Any]] = []
        self.error = error

    def users(self) -> "FakeGmail":
        return self

    def messages(self) -> "FakeGmail":
        return self

    def send(self, userId: str, body: dict[str, str]) -> "FakeGmail":
        self.sent.append({"userId": userId, **body})
        return self

    def execute(self) -> dict[str, str]:
        if self.error:
            raise self.error
        return {"id": "18f0c0ffee"}


def _sender(
    monkeypatch: pytest.MonkeyPatch, gmail: FakeGmail, tmp_path: Path
) -> gmail_client.GmailSender:
    monkeypatch.setattr(gmail_client, "load_credentials", lambda *_: object())
    monkeypatch.setattr(gmail_client, "build", lambda *a, **k: gmail)
    return gmail_client.GmailSender(tmp_path / "credentials.json", tmp_path / "token.json")


def test_send_posts_a_raw_rfc822_message_as_the_authorized_user(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    gmail = FakeGmail()

    message_id = _sender(monkeypatch, gmail, tmp_path).send(
        "4x2kms@gmail.com", "Review needed", "Body text."
    )

    [request] = gmail.sent
    assert request["userId"] == "me"
    message = message_from_bytes(base64.urlsafe_b64decode(request["raw"]))
    assert (message["To"], message["Subject"], message["Message-ID"]) == (
        "4x2kms@gmail.com", "Review needed", message_id)  # fmt: skip
    assert message["From"] is None  # Gmail fills in the authorized account
    assert message.get_payload(decode=True).decode().strip() == "Body text."  # type: ignore[union-attr]


def test_api_rejection_is_a_delivery_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    refused = HttpError(
        httplib2.Response({"status": "403"}), b'{"error": "insufficientPermissions"}'
    )

    with pytest.raises(EmailDeliveryError, match="refused"):
        _sender(monkeypatch, FakeGmail(error=refused), tmp_path).send("a@b.co", "s", "b")


def test_without_a_cached_token_it_asks_for_authorization(tmp_path: Path) -> None:
    sender = gmail_client.GmailSender(tmp_path / "credentials.json", tmp_path / "token.json")

    with pytest.raises(EmailDeliveryError, match="gmail_client authorize"):
        sender.send("a@b.co", "s", "b")


def test_expired_token_is_refreshed_and_saved(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class FakeCredentials:
        valid, expired, refresh_token = False, True, "refresh-me"

        def refresh(self, request: Any) -> None:
            self.valid = True

        def to_json(self) -> str:
            return json.dumps({"token": "fresh"})

    token = tmp_path / "token.json"
    token.write_text("{}")
    monkeypatch.setattr(Credentials, "from_authorized_user_file",
                        lambda *a, **k: FakeCredentials())  # fmt: skip

    creds = gmail_client.load_credentials(tmp_path / "credentials.json", token)

    assert creds.valid and json.loads(token.read_text()) == {"token": "fresh"}


def test_authorize_needs_the_oauth_client_file(tmp_path: Path) -> None:
    with pytest.raises(EmailDeliveryError, match=r"setup_gmail\.md"):
        gmail_client.authorize(tmp_path / "missing.json", tmp_path / "token.json")


def test_only_the_send_scope_is_requested() -> None:
    assert gmail_client.SCOPES == ["https://www.googleapis.com/auth/gmail.send"]
