"""Phase 5 — customs_bulletin_mcp: every tool matches the direct service call byte for byte,
and change detection stays idempotent through MCP."""

from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from mcp_servers.customs_bulletin_mcp.server import mcp
from services import bulletin_service as svc
from tests.fixtures.compliance import BASELINE_BULLETIN, edit_section
from tests.test_mcp_servers.conftest import assert_parity, assert_tool_error, connect, structured

TOOLS_TESTED = {
    "get_current_snapshot",
    "diff_since_last_scan",
    "get_section_changes",
    "get_change",
    "list_changes",
    "list_snapshots",
    "get_publication",
    "update_publication",
}
SCANNER = "watch-scheduler"
DUTY_RAISED = edit_section(
    BASELINE_BULLETIN, "HSN-8708-duty", "15% of the assessable", "20% of the assessable"
)


def _scan(session_factory: sessionmaker[Session]) -> None:
    """The scheduler's capture step (not a tool — Option A)."""
    with session_factory() as session, session.begin():
        svc.capture_snapshot(session, SCANNER)


async def test_scan_cycle_over_mcp_matches_the_service(
    seeded: None, direct: Any, session_factory: sessionmaker[Session]
) -> None:
    _scan(session_factory)
    async with connect(mcp) as client:
        published = await client.call_tool("update_publication", {"content": DUTY_RAISED})
        publication = await client.call_tool("get_publication", {})
        _scan(session_factory)
        snapshot = await client.call_tool("get_current_snapshot", {})
        snapshots = await client.call_tool("list_snapshots", {"limit": 5})
        detected = await client.call_tool(
            "diff_since_last_scan", {}, meta={"actor": "regulatory_watch_agent"}
        )
        repeat = await client.call_tool("diff_since_last_scan", {})
        change_id = structured(detected)["result"]["id"]
        change = await client.call_tool("get_change", {"change_id": change_id})
        sections = await client.call_tool("get_section_changes", {"change_id": change_id})
        changes = await client.call_tool("list_changes", {})

    assert_parity(published, direct(svc.get_publication))
    assert_parity(publication, direct(svc.get_publication))
    assert_parity(snapshot, direct(svc.get_latest_snapshot))
    assert_parity(snapshots, direct(lambda s: svc.list_snapshots(s, 5)))
    assert_parity(detected, direct(lambda s: svc.get_change(s, change_id)))
    assert structured(repeat) == {"result": None}  # idempotent through MCP as well
    assert_parity(change, direct(lambda s: svc.get_change(s, change_id)))
    assert_parity(sections, direct(lambda s: svc.section_changes(s, change_id)))
    assert [c["rule_key"] for c in structured(sections)["result"]] == ["HSN-8708-duty"]
    assert_parity(changes, direct(svc.list_changes))


async def test_publishing_through_mcp_captures_nothing(seeded: None, direct: Any) -> None:
    async with connect(mcp) as client:
        await client.call_tool("update_publication", {"content": DUTY_RAISED})
        snapshot = await client.call_tool("get_current_snapshot", {})
        nothing = await client.call_tool("diff_since_last_scan", {})

    assert_tool_error(snapshot, "BulletinSnapshotNotFoundError")
    assert structured(nothing) == {"result": None}
    assert direct(lambda s: svc.list_snapshots(s)) == []


async def test_malformed_bulletin_is_refused(seeded: None, direct: Any) -> None:
    async with connect(mcp) as client:
        result = await client.call_tool("update_publication", {"content": "no sections here"})

    assert_tool_error(result, "InvalidBulletinError")
    assert direct(svc.get_publication).content == BASELINE_BULLETIN
