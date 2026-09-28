"""Phase 5 — notification_service: review email composition, sending and auditing."""

from datetime import date
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from models.db_models import AuditEvent
from models.enums import RuleDecision
from models.schemas import ComplianceRuleRecord, RuleProposal
from services import compliance_service
from services import notification_service as svc
from services.errors import EmailDeliveryError, RuleNotFoundError

REVIEWER = "reviewer@example.com"


class RecordingSender:
    backend = "test"

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    def send(self, to: str, subject: str, body: str) -> str:
        self.sent.append((to, subject, body))
        return f"<{len(self.sent)}@test>"


class FailingSender:
    backend = "test"

    def send(self, to: str, subject: str, body: str) -> str:
        raise TimeoutError("backend timed out")


def _pending_rule(session: Session) -> ComplianceRuleRecord:
    base = compliance_service.propose_change(
        session,
        RuleProposal(rule_key="HSN-8708-duty", content="BCD 15%.", hs_codes=["8708"],
                     effective_date=date(2026, 4, 1)),
        "seed",
    )  # fmt: skip
    compliance_service.decide(session, base.id, RuleDecision.APPROVE, "a.sharma")
    return compliance_service.propose_change(
        session,
        RuleProposal(rule_key="HSN-8708-duty", content="BCD 20%.", hs_codes=["8708"],
                     effective_date=date(2026, 10, 1)),
        "regulatory_watch_agent",
    )  # fmt: skip


def test_review_body_carries_the_rule_and_the_change(db_session: Session) -> None:
    rule = _pending_rule(db_session)

    body = svc.compose_review_body(rule, "Substantive change.\n- HSN-8708-duty: modified")

    for expected in ("HSN-8708-duty v2 (pending)", "Effective: 2026-10-01", "HS codes: 8708",
                     "- HSN-8708-duty: modified", "BCD 20%.", "Compliance dashboard",
                     str(rule.id)):  # fmt: skip
        assert expected in body


def test_send_returns_a_receipt_and_audits_it(db_session: Session) -> None:
    rule = _pending_rule(db_session)
    sender = RecordingSender()

    receipt = svc.send_review_email(
        db_session, to=REVIEWER, subject="Review HSN-8708-duty", diff_summary="changed",
        rule_id=rule.id, sender=sender, actor="regulatory_watch_agent",
    )  # fmt: skip

    assert (receipt.message_id, receipt.backend, receipt.rule_id) == ("<1@test>", "test", rule.id)
    [(to, subject, body)] = sender.sent
    assert (to, subject) == (REVIEWER, "Review HSN-8708-duty")
    assert body == svc.compose_review_body(rule, "changed")
    event = db_session.scalars(
        select(AuditEvent).where(AuditEvent.action == "notification.review_email_sent")
    ).one()
    assert (event.actor, event.entity_id) == ("regulatory_watch_agent", rule.id)


def test_backend_failure_raises_typed_error_and_audits_nothing(db_session: Session) -> None:
    rule = _pending_rule(db_session)

    with pytest.raises(EmailDeliveryError, match="backend timed out"):
        svc.send_review_email(
            db_session, to=REVIEWER, subject="s", diff_summary="d", rule_id=rule.id,
            sender=FailingSender(), actor="agent",
        )  # fmt: skip

    actions = db_session.scalars(select(AuditEvent.action)).all()
    assert "notification.review_email_sent" not in actions


def test_unknown_rule_raises_before_sending(db_session: Session) -> None:
    sender = RecordingSender()

    with pytest.raises(RuleNotFoundError):
        svc.send_review_email(
            db_session, to=REVIEWER, subject="s", diff_summary="d", rule_id=uuid4(),
            sender=sender, actor="agent",
        )  # fmt: skip
    assert sender.sent == []
