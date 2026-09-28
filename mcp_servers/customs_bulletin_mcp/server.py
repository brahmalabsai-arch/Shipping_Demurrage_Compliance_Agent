"""customs_bulletin_mcp — the simulated customs bulletin as seen by scheduled scans (arch §5).

A thin adapter over ``services.bulletin_service``. The watch agent's view of the bulletin is
the scans: ``get_current_snapshot`` is the latest *captured* snapshot, and
``diff_since_last_scan`` logs the change between the last two scans (``None`` when there is
nothing new — calling it again never duplicates a detection). Capturing is not a tool: only the
scheduler does it (edge cases, Phase 6, Option A). ``get_publication`` / ``update_publication``
are the publisher's side, for operators — not for the watch agent.
Run: ``python -m mcp_servers.customs_bulletin_mcp.server`` (stdio).
"""

from uuid import UUID

from mcp_servers.common import ToolContext, configure_logging, create_server, run_tool
from models.schemas import (
    BulletinPublicationRecord,
    BulletinSnapshotRecord,
    ChangeLogEntryRecord,
    SectionChange,
)
from services import bulletin_service as svc

mcp = create_server(
    "customs_bulletin_mcp",
    "The simulated external customs bulletin, as captured by scheduled scans. "
    "diff_since_last_scan reports a change between the last two scans once; get_section_changes "
    "breaks a change down per rule_key (one proposal per substantively changed rule).",
)


@mcp.tool()
async def get_current_snapshot(ctx: ToolContext) -> BulletinSnapshotRecord:
    """The bulletin as captured by the most recent scheduled scan."""
    return await run_tool(ctx, "get_current_snapshot", {},
                          lambda s, _: svc.get_latest_snapshot(s))  # fmt: skip


@mcp.tool()
async def diff_since_last_scan(ctx: ToolContext) -> ChangeLogEntryRecord | None:
    """Log and return the change between the last two scans, or null if there is none or it
    was already reported."""
    return await run_tool(ctx, "diff_since_last_scan", {},
                          lambda s, c: svc.diff_since_last(s, c.actor))  # fmt: skip


@mcp.tool()
async def get_section_changes(ctx: ToolContext, change_id: UUID) -> list[SectionChange]:
    """Per-rule breakdown (added / modified / removed, substantive or not) of a detected change."""
    return await run_tool(ctx, "get_section_changes", {"change_id": change_id},
                          lambda s, _: svc.section_changes(s, change_id))  # fmt: skip


@mcp.tool()
async def get_change(ctx: ToolContext, change_id: UUID) -> ChangeLogEntryRecord:
    """One detected change by id."""
    return await run_tool(ctx, "get_change", {"change_id": change_id},
                          lambda s, _: svc.get_change(s, change_id))  # fmt: skip


@mcp.tool()
async def list_changes(ctx: ToolContext) -> list[ChangeLogEntryRecord]:
    """Every detected change, newest first."""
    return await run_tool(ctx, "list_changes", {}, lambda s, _: svc.list_changes(s))


@mcp.tool()
async def list_snapshots(
    ctx: ToolContext, limit: int | None = None
) -> list[BulletinSnapshotRecord]:
    """Scan snapshots, newest first."""
    return await run_tool(ctx, "list_snapshots", {"limit": limit},
                          lambda s, _: svc.list_snapshots(s, limit))  # fmt: skip


@mcp.tool()
async def get_publication(ctx: ToolContext) -> BulletinPublicationRecord:
    """Publisher's view: the bulletin text as published right now (not yet necessarily scanned)."""
    return await run_tool(ctx, "get_publication", {}, lambda s, _: svc.get_publication(s))


@mcp.tool()
async def update_publication(ctx: ToolContext, content: str) -> BulletinPublicationRecord:
    """Publisher's side: replace the bulletin text (sectioned format). Triggers no scan."""
    return await run_tool(ctx, "update_publication", {"content": content},
                          lambda s, c: svc.publish(s, content, c.actor))  # fmt: skip


def main() -> None:
    configure_logging()
    mcp.run()


if __name__ == "__main__":
    main()
