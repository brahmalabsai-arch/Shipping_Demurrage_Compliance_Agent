"""Phase 5 — email backends: the local outbox sender and backend selection."""

from email import message_from_bytes
from pathlib import Path

import pytest
from pydantic import SecretStr

from config.settings import Settings
from integrations import email
from services.errors import EmailDeliveryError


def test_outbox_sender_writes_a_complete_rfc822_message(tmp_path: Path) -> None:
    sender = email.OutboxSender(tmp_path / "outbox")

    message_id = sender.send("reviewer@example.com", "Review ORG-KR-CEPA", "Body line.\nSecond.")

    [path] = list((tmp_path / "outbox").glob("*.eml"))
    message = message_from_bytes(path.read_bytes())
    assert message["Message-ID"] == message_id
    assert (message["To"], message["Subject"], message["From"]) == (
        "reviewer@example.com",
        "Review ORG-KR-CEPA",
        email.FROM_ADDRESS,
    )
    assert message.get_payload(decode=True).decode().strip() == "Body line.\nSecond."  # type: ignore[union-attr]


def test_unwritable_outbox_is_a_delivery_error(tmp_path: Path) -> None:
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")

    with pytest.raises(EmailDeliveryError):
        email.OutboxSender(blocker).send("a@b.co", "s", "b")


def test_backend_is_chosen_from_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    password = SecretStr("unused")
    outbox = Settings(_env_file=None, postgres_password=password, outbox_path=tmp_path)
    gmail = Settings(_env_file=None, postgres_password=password, email_backend="gmail")

    monkeypatch.setattr(email, "get_settings", lambda: outbox)
    sender = email.get_email_sender()
    assert isinstance(sender, email.OutboxSender) and sender.directory == tmp_path

    monkeypatch.setattr(email, "get_settings", lambda: gmail)
    with pytest.raises(EmailDeliveryError, match="Phase 6"):
        email.get_email_sender()
