"""Phase 5 — compliance_rulebook_mcp: every tool matches the direct service call byte for byte,
and the governance rules hold over MCP too (pending on propose, named reviewer on decide)."""

from typing import Any

import pytest

from mcp_servers.compliance_rulebook_mcp.server import mcp
from models.enums import RuleStatus
from services import compliance_service as svc
from tests.test_mcp_servers.conftest import assert_parity, assert_tool_error, connect, structured

TOOLS_TESTED = {
    "search_rules",
    "list_approved",
    "list_pending",
    "list_rules",
    "get_rule",
    "get_current_rule",
    "propose_change",
    "decide",
    "get_review_context",
    "list_bulletin_drift",
    "revoke_decision",
}
KEY = "HSN-8708-duty"

READS: list[tuple[str, dict[str, Any], Any]] = [
    ("search_rules", {"query": "certificate of origin for Korean gearboxes", "hs_code": "87084000"},
     lambda s: svc.search_rules(s, "certificate of origin for Korean gearboxes", "87084000")),
    ("search_rules", {"query": "exchange rate", "limit": 1},
     lambda s: svc.search_rules(s, "exchange rate", limit=1)),
    ("list_approved", {}, lambda s: svc.list_approved(s)),
    ("list_approved", {"hs_code": "87083000"}, lambda s: svc.list_approved(s, "87083000")),
    ("list_pending", {}, lambda s: svc.list_pending(s)),
    ("list_rules", {"rule_key": KEY}, lambda s: svc.list_rules(s, KEY)),
    ("list_rules", {"status": "approved"}, lambda s: svc.list_rules(s, status=RuleStatus.APPROVED)),
    ("get_current_rule", {"rule_key": KEY}, lambda s: svc.get_current_rule(s, KEY)),
]  # fmt: skip


@pytest.mark.parametrize(("tool", "arguments", "service_call"), READS)
async def test_read_tools_match_the_service(
    seeded: None, direct: Any, tool: str, arguments: dict[str, Any], service_call: Any
) -> None:
    async with connect(mcp) as client:
        result = await client.call_tool(tool, arguments)

    assert_parity(result, direct(service_call))


async def test_search_finds_the_relevant_approved_rule(seeded: None) -> None:
    async with connect(mcp) as client:
        matches = structured(await client.call_tool(
            "search_rules", {"query": "Korean certificate of origin", "hs_code": "87084000"}))["result"]  # fmt: skip

    assert matches[0]["rule"]["rule_key"] == "ORG-KR-CEPA"
    assert {m["method"] for m in matches} == {"hybrid"}
    assert all(m["rule"]["status"] == "approved" for m in matches)


async def test_propose_then_decide_over_mcp(seeded: None, direct: Any) -> None:
    current = direct(lambda s: svc.get_current_rule(s, KEY))
    proposal = {"rule_key": KEY, "content": current.content.replace("15%", "20%"),
                "hs_codes": ["8708"], "effective_date": "2026-10-01",
                "change_summary": "BCD raised to 20%"}  # fmt: skip
    async with connect(mcp) as client:
        proposed = structured(await client.call_tool(
            "propose_change", {"proposal": proposal}, meta={"actor": "regulatory_watch_agent"}))  # fmt: skip
        assert_parity(await client.call_tool("list_pending", {}), direct(svc.list_pending))
        assert_parity(
            await client.call_tool("get_rule", {"rule_id": proposed["id"]}),
            direct(lambda s: svc.get_rule(s, proposed["id"])),
        )
        anonymous = await client.call_tool(
            "decide", {"rule_id": proposed["id"], "decision": "approve", "reviewer": " "}
        )
        approved = await client.call_tool(
            "decide", {"rule_id": proposed["id"], "decision": "approve", "reviewer": "a.sharma"}
        )
        again = await client.call_tool(
            "decide", {"rule_id": proposed["id"], "decision": "reject", "reviewer": "b.rao"}
        )

    assert proposed["status"] == "pending" and proposed["version"] == 2
    assert_tool_error(anonymous, "InvalidDecisionError")
    assert_parity(approved, direct(lambda s: svc.get_current_rule(s, KEY)))
    assert structured(approved)["decided_by"] == "a.sharma"
    assert_tool_error(again, "RuleAlreadyDecidedError")


async def test_a_proposal_cannot_smuggle_in_a_status(seeded: None, direct: Any) -> None:
    async with connect(mcp) as client:
        result = await client.call_tool("propose_change", {"proposal": {
            "rule_key": KEY, "content": "x", "effective_date": "2026-10-01",
            "status": "approved"}})  # fmt: skip

    assert result.isError
    assert direct(lambda s: svc.list_pending(s)) == []


async def test_review_context_and_drift_over_mcp(
    seeded: None, direct: Any, session_factory: Any
) -> None:
    from services import bulletin_service
    from tests.fixtures.compliance import BASELINE_BULLETIN, edit_section

    with session_factory() as session, session.begin():
        bulletin_service.publish(session, edit_section(
            BASELINE_BULLETIN, KEY, "15% of the assessable", "20% of the assessable"), "editor")  # fmt: skip
        bulletin_service.capture_snapshot(session, "scan")
    current = direct(lambda s: svc.get_current_rule(s, KEY))
    proposal = {"rule_key": KEY, "content": current.content.replace("15%", "20%"),
                "hs_codes": ["8708"], "effective_date": "2026-04-01"}  # fmt: skip
    async with connect(mcp) as client:
        drift_before = await client.call_tool("list_bulletin_drift", {})
        assert_parity(drift_before, direct(svc.bulletin_drift))
        proposed = structured(await client.call_tool("propose_change", {"proposal": proposal}))
        rejected = structured(await client.call_tool("decide", {
            "rule_id": proposed["id"], "decision": "reject", "reviewer": "a.sharma",
            "note": "awaiting notification"}))  # fmt: skip
        context = await client.call_tool("get_review_context", {"rule_id": proposed["id"]})
        drift_after = await client.call_tool("list_bulletin_drift", {})

    assert structured(drift_before)["result"][0]["rule_key"] == KEY
    assert rejected["decision_note"] == "awaiting notification"
    assert_parity(context, direct(lambda s: svc.review_context(s, proposed["id"])))
    assert structured(context)["in_force"]["version"] == 1
    assert_parity(drift_after, direct(svc.bulletin_drift))
    assert structured(drift_after)["result"][0]["rejected_same"]["decision_note"] == (
        "awaiting notification"
    )


async def test_revoke_decision_over_mcp(seeded: None, direct: Any) -> None:
    v1 = direct(lambda s: svc.get_current_rule(s, KEY))
    async with connect(mcp) as client:
        reasonless = await client.call_tool(
            "revoke_decision", {"rule_id": str(v1.id), "reviewer": "a.sharma", "reason": " "}
        )
        revoked = await client.call_tool(
            "revoke_decision",
            {"rule_id": str(v1.id), "reviewer": "a.sharma", "reason": "seeded in error"},
        )
        again = await client.call_tool(
            "revoke_decision", {"rule_id": str(v1.id), "reviewer": "a.sharma", "reason": "x"}
        )

    assert_tool_error(reasonless, "InvalidDecisionError")
    assert_parity(revoked, direct(lambda s: svc.get_rule(s, v1.id)))
    assert structured(revoked)["status"] == "revoked"
    assert structured(revoked)["decided_by"] == v1.decided_by
    assert_tool_error(again, "RuleNotInForceError")
