"""Phase 5 — notifications_mcp: the review email goes through notification_service with the
configured sender (the local outbox here), is audited, and failures come back typed."""

from email import message_from_bytes
from email.message import Message
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from config.settings import get_settings
from integrations import email
from mcp_servers.notifications_mcp.server import mcp
from models.db_models import AuditEvent
from services import compliance_service, notification_service
from tests.test_mcp_servers.conftest import assert_tool_error, connect, structured

TOOLS_TESTED = {"send_review_email"}
REVIEWER = "reviewer@example.com"


@pytest.fixture
def outbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(get_settings(), "outbox_path", tmp_path / "outbox")
    return tmp_path / "outbox"


def _only_message(outbox: Path) -> Message:
    [path] = list(outbox.glob("*.eml"))
    return message_from_bytes(path.read_bytes())


async def test_send_review_email_writes_the_message_and_audits_it(
    seeded: None, direct: Any, outbox: Path
) -> None:
    rule = direct(lambda s: compliance_service.get_current_rule(s, "ORG-KR-CEPA"))
    arguments = {"to": REVIEWER, "subject": "Review ORG-KR-CEPA", "rule_id": str(rule.id),
                 "diff_summary": "Substantive change.\n- ORG-KR-CEPA: modified"}  # fmt: skip
    async with connect(mcp) as client:
        receipt = structured(await client.call_tool(
            "send_review_email", arguments, meta={"actor": "regulatory_watch_agent"}))  # fmt: skip

    message = _only_message(outbox)
    assert (receipt["backend"], receipt["to"], receipt["rule_id"]) == (
        "outbox",
        REVIEWER,
        str(rule.id),
    )
    assert message["Message-ID"] == receipt["message_id"]
    assert (message["To"], message["Subject"]) == (REVIEWER, "Review ORG-KR-CEPA")
    body = message.get_payload(decode=True).decode()  # type: ignore[union-attr]
    expected = notification_service.compose_review_body(rule, arguments["diff_summary"])
    assert body.replace("\r\n", "\n").strip() == expected.strip()  # same body the service composes
    event = direct(lambda s: s.scalars(
        select(AuditEvent).where(AuditEvent.action == "notification.review_email_sent")).one())  # fmt: skip
    assert (event.actor, event.payload["receipt"]) == ("regulatory_watch_agent", receipt)


async def test_delivery_failure_is_typed_and_not_audited(
    seeded: None, direct: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    class BrokenSender:
        backend = "broken"

        def send(self, to: str, subject: str, body: str) -> str:
            raise ConnectionError("SMTP relay unreachable")

    monkeypatch.setattr(email, "get_email_sender", BrokenSender)
    rule = direct(lambda s: compliance_service.get_current_rule(s, "ORG-KR-CEPA"))
    async with connect(mcp) as client:
        result = await client.call_tool("send_review_email", {
            "to": REVIEWER, "subject": "s", "diff_summary": "d", "rule_id": str(rule.id)})  # fmt: skip

    assert "SMTP relay unreachable" in assert_tool_error(result, "EmailDeliveryError")
    assert (
        direct(lambda s: s.scalars(select(AuditEvent.action)).all()).count(
            "notification.review_email_sent"
        )
        == 0
    )


async def test_bad_address_and_unknown_rule_are_refused(seeded: None, outbox: Path) -> None:
    async with connect(mcp) as client:
        bad_address = await client.call_tool("send_review_email", {
            "to": "not-an-address", "subject": "s", "diff_summary": "d",
            "rule_id": "00000000-0000-0000-0000-000000000000"})  # fmt: skip
        unknown_rule = await client.call_tool("send_review_email", {
            "to": REVIEWER, "subject": "s", "diff_summary": "d",
            "rule_id": "00000000-0000-0000-0000-000000000000"})  # fmt: skip

    assert bad_address.isError and "to" in bad_address.content[0].text  # type: ignore[union-attr]
    assert_tool_error(unknown_rule, "RuleNotFoundError")
    assert not outbox.exists() or not list(outbox.glob("*.eml"))
