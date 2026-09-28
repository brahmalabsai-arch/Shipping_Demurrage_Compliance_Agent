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
from models.schemas import ComplianceRuleRecord, EmailReceipt, ReviewContext
from services import audit_service, bulletin_service, compliance_service
from services.errors import EmailDeliveryError


def _meta(rule: ComplianceRuleRecord) -> str:
    return (
        f"effective {rule.effective_date.isoformat()}, HS codes {', '.join(rule.hs_codes) or 'all'}"
    )


def compose_review_body(context: ReviewContext, diff_summary: str) -> str:
    """Plain-text body of the review email: the change measured against the rule in force,
    what happened in between, and both texts."""
    rule, in_force = context.proposal, context.in_force
    if in_force is None:
        against = "This is a new rule: nothing is approved for it yet."
    else:
        against = f"Compared with the rule in force, v{in_force.version} ({_meta(in_force)}):"
    lines = [
        "A compliance rule change is awaiting your review.",
        "",
        f"Rule: {rule.rule_key} v{rule.version} ({rule.status.value}; {_meta(rule)})",
        "",
        "What would change if you approve it",
        against,
        diff_summary.strip(),
    ]
    if context.history:
        lines += ["", "History since the rule in force:", *(f"- {h}" for h in context.history)]
    lines += ["", f"Proposed text (v{rule.version}):", rule.content]
    if in_force is not None:
        lines += ["", f"Text in force (v{in_force.version}):", in_force.content]
    lines += [
        "",
        "Approve or reject it on the Compliance dashboard (Pending review tab); a rejection needs",
        "a reason. The rulebook does not change until a reviewer decides.",
        f"Rule id: {rule.id}",
    ]
    return "\n".join(lines)


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
    ``notification.review_email_sent``. ``diff_summary`` should describe the change against the
    rule in force; the email adds that version, the history since it, and both texts. If the
    rule came from a detected bulletin change, that change is stamped ``emailed_at`` in the same
    transaction (first email only).

    Raises ``RuleNotFoundError`` for an unknown rule and ``EmailDeliveryError`` if the backend
    fails — nothing is audited then, so the caller (the watch agent) records the failure in its
    own transaction and keeps the pending rule (architecture §7).
    """
    context = compliance_service.review_context(session, rule_id)
    rule = context.proposal
    body = compose_review_body(context, diff_summary)
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
    if rule.source_diff_id is not None:
        bulletin_service.mark_emailed(session, rule.source_diff_id, actor)
    session.flush()
    return receipt
