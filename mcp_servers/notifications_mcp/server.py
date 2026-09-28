"""notifications_mcp — reviewer email (architecture §5, §7).

A thin adapter over ``services.notification_service`` with the sender chosen in settings
(``EMAIL_BACKEND``: local ``outbox`` now, real Gmail from Phase 6). Run: ``python -m
mcp_servers.notifications_mcp.server`` (stdio).
"""

from uuid import UUID

from integrations import email
from mcp_servers.common import ToolContext, configure_logging, create_server, run_tool
from models.schemas import EmailAddress, EmailReceipt
from services import notification_service as svc

mcp = create_server(
    "notifications_mcp",
    "Emails a human reviewer about a compliance rule change awaiting decision. The email only "
    "informs; the reviewer decides on the Compliance dashboard.",
)


@mcp.tool()
async def send_review_email(
    ctx: ToolContext, to: EmailAddress, subject: str, diff_summary: str, rule_id: UUID
) -> EmailReceipt:
    """Email a reviewer the details of a pending rule change; fails with EmailDeliveryError if
    the mail backend cannot send."""
    arguments = {"to": to, "subject": subject, "diff_summary": diff_summary, "rule_id": rule_id}
    return await run_tool(
        ctx, "send_review_email", arguments,
        lambda s, c: svc.send_review_email(
            s, to=to, subject=subject, diff_summary=diff_summary, rule_id=rule_id,
            sender=email.get_email_sender(), actor=c.actor,
        ),
    )  # fmt: skip


def main() -> None:
    configure_logging()
    mcp.run()


if __name__ == "__main__":
    main()
