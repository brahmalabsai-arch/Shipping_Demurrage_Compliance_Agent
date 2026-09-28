"""Reviewer notifications: the email that asks a human to decide on a pending rule change.

The single code path behind ``notifications_mcp.send_review_email``. The email only *informs*;
it carries no approval link that could change state — the reviewer decides on the Compliance
dashboard (``compliance_service.decide``). The sender is injected, so the same code runs with
the local outbox backend, the Gmail backend (Phase 6) or a test double.
"""

from uuid import UUID

from sqlalchemy.orm import Session

from integrations.email import EmailSender
from models.db_models import utcnow
from models.schemas import ComplianceRuleRecord, EmailReceipt
from services import audit_service, compliance_service
from services.errors import EmailDeliveryError


def compose_review_body(rule: ComplianceRuleRecord, diff_summary: str) -> str:
    """Plain-text body of the review email for ``rule``."""
    return "\n".join(
        [
            "A compliance rule change is awaiting your review.",
            "",
            f"Rule: {rule.rule_key} v{rule.version} ({rule.status.value})",
            f"Effective: {rule.effective_date.isoformat()}",
            f"HS codes: {', '.join(rule.hs_codes) or 'all'}",
            "",
            "What changed in the customs bulletin:",
            diff_summary.strip(),
            "",
            "Proposed rule text:",
            rule.content,
            "",
            "Approve or reject it on the Compliance dashboard (Pending review tab). The rulebook",
            "does not change until a reviewer decides.",
            f"Rule id: {rule.id}",
        ]
    )


def send_review_email(
    session: Session,
    *,
    to: str,
    subject: str,
    diff_summary: str,
    rule_id: UUID,
    sender: EmailSender,
    actor: str,
) -> EmailReceipt:
    """Email a reviewer about a rule awaiting decision; audited as
    ``notification.review_email_sent``.

    Raises ``RuleNotFoundError`` for an unknown rule and ``EmailDeliveryError`` if the backend
    fails — nothing is audited then, so the caller (the watch agent) records the failure in its
    own transaction and keeps the pending rule (architecture §7).
    """
    rule = compliance_service.get_rule(session, rule_id)
    body = compose_review_body(rule, diff_summary)
    try:
        message_id = sender.send(to, subject, body)
    except EmailDeliveryError:
        raise
    except Exception as exc:  # any backend failure becomes the one typed delivery error
        raise EmailDeliveryError(f"{sender.backend} backend failed: {exc}") from exc

    receipt = EmailReceipt(
        message_id=message_id,
        backend=sender.backend,
        to=to,
        subject=subject,
        rule_id=rule.id,
        sent_at=utcnow(),
    )
    audit_service.record(
        session,
        actor=actor,
        action="notification.review_email_sent",
        entity_type="compliance_rule",
        entity_id=rule.id,
        payload={"receipt": receipt.model_dump()},
    )
    session.flush()
    return receipt
