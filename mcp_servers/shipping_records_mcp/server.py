"""shipping_records_mcp — shipments, their documents and lifecycle status (architecture §5).

A thin adapter over ``services.shipping_service``: every tool is one service call, so results
are exactly what the Shipping Ops dashboard sees. Run: ``python -m
mcp_servers.shipping_records_mcp.server`` (stdio).
"""

from uuid import UUID

from mcp_servers.common import ToolContext, configure_logging, create_server, run_tool
from models.enums import ShipmentStatus
from models.schemas import (
    DocumentAmend,
    DocumentCreate,
    DocumentRecord,
    ShipmentCreate,
    ShipmentRecord,
    ShipmentUpdate,
)
from services import shipping_service as svc

mcp = create_server(
    "shipping_records_mcp",
    (
        "Shipment records for the Nhava Sheva (INNSA) import desk: shipments, their commercial "
        "documents and lifecycle status. Agents analysing a shipment should read it with "
        "get_active_shipment, which fails if the shipment was cancelled."
    ),
)


@mcp.tool()
async def list_shipments(
    ctx: ToolContext, status: ShipmentStatus | None = None
) -> list[ShipmentRecord]:
    """List shipments, optionally only those in one lifecycle status."""
    return await run_tool(ctx, "list_shipments", {"status": status},
                          lambda s, _: svc.list_shipments(s, status))  # fmt: skip


@mcp.tool()
async def get_shipment(ctx: ToolContext, shipment_id: UUID) -> ShipmentRecord:
    """Get one shipment by id, whatever its status."""
    return await run_tool(ctx, "get_shipment", {"shipment_id": shipment_id},
                          lambda s, _: svc.get_shipment(s, shipment_id))  # fmt: skip


@mcp.tool()
async def get_shipment_by_container(ctx: ToolContext, container_id: str) -> ShipmentRecord:
    """Get one shipment by its ISO 6346 container number (e.g. MSKU7001001)."""
    return await run_tool(ctx, "get_shipment_by_container", {"container_id": container_id},
                          lambda s, _: svc.get_shipment_by_container_id(s, container_id))  # fmt: skip


@mcp.tool()
async def get_active_shipment(ctx: ToolContext, shipment_id: UUID) -> ShipmentRecord:
    """Get a shipment for analysis; fails with ShipmentCancelledError if it was cancelled."""
    return await run_tool(ctx, "get_active_shipment", {"shipment_id": shipment_id},
                          lambda s, _: svc.get_active_shipment(s, shipment_id))  # fmt: skip


@mcp.tool()
async def get_documents(
    ctx: ToolContext, shipment_id: UUID, include_superseded: bool = False
) -> list[DocumentRecord]:
    """A shipment's documents — by default only the current version of each."""
    return await run_tool(
        ctx, "get_documents", {"shipment_id": shipment_id, "include_superseded": include_superseded},
        lambda s, _: svc.get_documents(s, shipment_id, include_superseded),
    )  # fmt: skip


@mcp.tool()
async def create_shipment(ctx: ToolContext, shipment: ShipmentCreate) -> ShipmentRecord:
    """Create a shipment, starting as a quotation ('quoted') or a direct booking ('booked')."""
    return await run_tool(ctx, "create_shipment", {"shipment": shipment},
                          lambda s, c: svc.create_shipment(s, shipment, c.actor))  # fmt: skip


@mcp.tool()
async def update_shipment(
    ctx: ToolContext, shipment_id: UUID, changes: ShipmentUpdate
) -> ShipmentRecord:
    """Edit a shipment's descriptive fields; only the fields given are changed."""
    return await run_tool(ctx, "update_shipment", {"shipment_id": shipment_id, "changes": changes},
                          lambda s, c: svc.update_shipment(s, shipment_id, changes, c.actor))  # fmt: skip


@mcp.tool()
async def update_status(
    ctx: ToolContext, shipment_id: UUID, status: ShipmentStatus
) -> ShipmentRecord:
    """Move a shipment to another lifecycle status; only allowed transitions succeed. Never to
    ``cleared`` — see ``clear_shipment``."""
    return await run_tool(ctx, "update_status", {"shipment_id": shipment_id, "status": status},
                          lambda s, c: svc.update_status(s, shipment_id, status, c.actor))  # fmt: skip


@mcp.tool()
async def clear_shipment(
    ctx: ToolContext, shipment_id: UUID, filing_decision_id: UUID
) -> ShipmentRecord:
    """File a shipment under review as ``cleared``, citing a reviewer's approved filing decision
    (taken at a supervised clearance run's filing gate) — the only way to clear a shipment."""
    arguments = {"shipment_id": shipment_id, "filing_decision_id": filing_decision_id}
    return await run_tool(
        ctx, "clear_shipment", arguments,
        lambda s, c: svc.clear_shipment(s, shipment_id, filing_decision_id, c.actor),
    )  # fmt: skip


@mcp.tool()
async def create_document(
    ctx: ToolContext, shipment_id: UUID, document: DocumentCreate
) -> DocumentRecord:
    """Attach a new document (version 1) to a shipment."""
    return await run_tool(ctx, "create_document", {"shipment_id": shipment_id, "document": document},
                          lambda s, c: svc.add_document(s, shipment_id, document, c.actor))  # fmt: skip


@mcp.tool()
async def update_document(
    ctx: ToolContext, document_id: UUID, changes: DocumentAmend
) -> DocumentRecord:
    """Re-issue a document: creates the next version and supersedes the one given."""
    return await run_tool(ctx, "update_document", {"document_id": document_id, "changes": changes},
                          lambda s, c: svc.amend_document(s, document_id, changes, c.actor))  # fmt: skip


def main() -> None:
    configure_logging()
    mcp.run()


if __name__ == "__main__":
    main()
