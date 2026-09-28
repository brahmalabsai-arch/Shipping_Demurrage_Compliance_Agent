"""compliance_rulebook_mcp — the governed compliance rulebook (architecture §5).

A thin adapter over ``services.compliance_service``. Searches and the rulebook only ever cover
**approved** rules; ``propose_change`` always stores ``pending``; ``decide`` (a named human
reviewer's final verdict) is the only path to ``approved``. Run: ``python -m
mcp_servers.compliance_rulebook_mcp.server`` (stdio).
"""

from uuid import UUID

from mcp_servers.common import ToolContext, configure_logging, create_server, run_tool
from models.enums import RuleDecision, RuleStatus
from models.schemas import (
    ComplianceRuleRecord,
    ReviewContext,
    RuleDrift,
    RuleMatch,
    RuleProposal,
)
from services import compliance_service as svc

mcp = create_server(
    "compliance_rulebook_mcp",
    "Customs compliance rules for imports into INNSA, versioned per rule_key. search_rules and "
    "list_approved return approved rules only. propose_change stages a pending version for "
    "human review; decide records a human reviewer's final approve/reject.",
)


@mcp.tool()
async def search_rules(
    ctx: ToolContext, query: str, hs_code: str | None = None, limit: int = 5
) -> list[RuleMatch]:
    """Approved rules relevant to a question, optionally only those covering an HS code."""
    return await run_tool(
        ctx, "search_rules", {"query": query, "hs_code": hs_code, "limit": limit},
        lambda s, _: svc.search_rules(s, query, hs_code, limit),
    )  # fmt: skip


@mcp.tool()
async def list_approved(ctx: ToolContext, hs_code: str | None = None) -> list[ComplianceRuleRecord]:
    """The rulebook in force: the approved version of every rule (optionally for an HS code)."""
    return await run_tool(ctx, "list_approved", {"hs_code": hs_code},
                          lambda s, _: svc.list_approved(s, hs_code))  # fmt: skip


@mcp.tool()
async def list_pending(ctx: ToolContext) -> list[ComplianceRuleRecord]:
    """Proposed rule versions awaiting a human decision, oldest first."""
    return await run_tool(ctx, "list_pending", {}, lambda s, _: svc.list_pending(s))


@mcp.tool()
async def list_rules(
    ctx: ToolContext, rule_key: str | None = None, status: RuleStatus | None = None
) -> list[ComplianceRuleRecord]:
    """Every version of every rule (optionally one rule and/or one status) — the history."""
    return await run_tool(ctx, "list_rules", {"rule_key": rule_key, "status": status},
                          lambda s, _: svc.list_rules(s, rule_key, status))  # fmt: skip


@mcp.tool()
async def get_rule(ctx: ToolContext, rule_id: UUID) -> ComplianceRuleRecord:
    """One rule version by id, whatever its status."""
    return await run_tool(ctx, "get_rule", {"rule_id": rule_id},
                          lambda s, _: svc.get_rule(s, rule_id))  # fmt: skip


@mcp.tool()
async def get_current_rule(ctx: ToolContext, rule_key: str) -> ComplianceRuleRecord:
    """The approved version of a rule currently in force."""
    return await run_tool(ctx, "get_current_rule", {"rule_key": rule_key},
                          lambda s, _: svc.get_current_rule(s, rule_key))  # fmt: skip


@mcp.tool()
async def propose_change(ctx: ToolContext, proposal: RuleProposal) -> ComplianceRuleRecord:
    """Stage a new version of a rule for human review. It is always stored as pending."""
    return await run_tool(ctx, "propose_change", {"proposal": proposal},
                          lambda s, c: svc.propose_change(s, proposal, c.actor))  # fmt: skip


@mcp.tool()
async def decide(
    ctx: ToolContext,
    rule_id: UUID,
    decision: RuleDecision,
    reviewer: str,
    note: str | None = None,
) -> ComplianceRuleRecord:
    """Record a human reviewer's final approve/reject of a pending rule — the only way a rule
    becomes approved. ``reviewer`` must name the person deciding; a rejection needs a ``note``
    giving the reason (optional for an approval)."""
    arguments = {"rule_id": rule_id, "decision": decision, "reviewer": reviewer, "note": note}
    return await run_tool(ctx, "decide", arguments,
                          lambda s, _: svc.decide(s, rule_id, decision, reviewer, note))  # fmt: skip


@mcp.tool()
async def get_review_context(ctx: ToolContext, rule_id: UUID) -> ReviewContext:
    """What a reviewer judges a proposal against: the rule in force it would replace and the
    history since (other proposals — pending, or rejected and why)."""
    return await run_tool(ctx, "get_review_context", {"rule_id": rule_id},
                          lambda s, _: svc.review_context(s, rule_id))  # fmt: skip


@mcp.tool()
async def list_bulletin_drift(ctx: ToolContext) -> list[RuleDrift]:
    """Rules where the latest scanned bulletin differs from the approved rule in force, each
    marked if that exact text is already pending or was rejected before."""
    return await run_tool(ctx, "list_bulletin_drift", {}, lambda s, _: svc.bulletin_drift(s))


def main() -> None:
    configure_logging()
    mcp.run()


if __name__ == "__main__":
    main()
