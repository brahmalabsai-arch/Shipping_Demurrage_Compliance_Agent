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
    assert {m["method"] for m in matches} == {"keyword"}
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
