"""Phase 5 — shipping_records_mcp: every tool matches the direct service call byte for byte."""

from collections.abc import Callable
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from mcp_servers.shipping_records_mcp.server import mcp
from models.db_models import AuditEvent
from models.enums import DocType, ShipmentStatus
from services import shipping_service as svc
from tests.fixtures.governance import approve_filing
from tests.fixtures.shipments import make_shipment
from tests.test_mcp_servers.conftest import assert_parity, assert_tool_error, connect, structured

TOOLS_TESTED = {
    "list_shipments",
    "get_shipment",
    "get_shipment_by_container",
    "get_active_shipment",
    "get_documents",
    "create_shipment",
    "update_shipment",
    "update_status",
    "create_document",
    "update_document",
    "clear_shipment",
}
CLEAN, DELAYED = "MSKU7001001", "HLXU7004004"
Ids = dict[str, Any]


def _ids(session: Session) -> Ids:
    return {
        "clean": svc.get_shipment_by_container_id(session, CLEAN).id,
        "delayed": svc.get_shipment_by_container_id(session, DELAYED).id,
    }


READS: list[tuple[str, Callable[[Ids], dict[str, Any]], Callable[[Session, Ids], Any]]] = [
    ("list_shipments", lambda i: {}, lambda s, i: svc.list_shipments(s)),
    ("list_shipments", lambda i: {"status": "held"},
     lambda s, i: svc.list_shipments(s, ShipmentStatus.HELD)),
    ("get_shipment", lambda i: {"shipment_id": str(i["clean"])},
     lambda s, i: svc.get_shipment(s, i["clean"])),
    ("get_shipment_by_container", lambda i: {"container_id": DELAYED},
     lambda s, i: svc.get_shipment_by_container_id(s, DELAYED)),
    ("get_active_shipment", lambda i: {"shipment_id": str(i["delayed"])},
     lambda s, i: svc.get_active_shipment(s, i["delayed"])),
    ("get_documents", lambda i: {"shipment_id": str(i["clean"])},
     lambda s, i: svc.get_documents(s, i["clean"])),
    ("get_documents", lambda i: {"shipment_id": str(i["clean"]), "include_superseded": True},
     lambda s, i: svc.get_documents(s, i["clean"], include_superseded=True)),
]  # fmt: skip


@pytest.mark.parametrize(("tool", "arguments", "service_call"), READS)
async def test_read_tools_match_the_service(
    seeded: None, direct: Any, tool: str, arguments: Any, service_call: Any
) -> None:
    ids = direct(_ids)
    async with connect(mcp) as client:
        result = await client.call_tool(tool, arguments(ids))

    assert_parity(result, direct(lambda s: service_call(s, ids)))


async def test_write_tools_persist_what_the_service_then_reads(direct: Any) -> None:
    new = make_shipment(container_id="TGHU7009009")
    meta = {"actor": "document_verification_agent", "run_id": "run-42"}
    async with connect(mcp) as client:
        created = structured(await client.call_tool(
            "create_shipment", {"shipment": new.model_dump(mode="json")}, meta=meta))  # fmt: skip
        shipment_id = created["id"]
        updated = await client.call_tool(
            "update_shipment",
            {"shipment_id": shipment_id, "changes": {"declared_qty": "1150"}},
            meta=meta,
        )
        moved = await client.call_tool(
            "update_status", {"shipment_id": shipment_id, "status": "booked"}, meta=meta
        )
        document = structured(await client.call_tool(
            "create_document",
            {"shipment_id": shipment_id,
             "document": {"doc_type": "invoice", "qty": "1150", "value": "80500.00",
                          "currency": "USD", "issued_date": "2026-08-18"}},
            meta=meta,
        ))  # fmt: skip
        amended = await client.call_tool(
            "update_document",
            {"document_id": document["id"], "changes": {"qty": "1200"}},
            meta=meta,
        )

    shipment = direct(lambda s: svc.get_shipment(s, shipment_id))
    assert_parity(moved, shipment)
    assert shipment.declared_qty == Decimal("1150") and shipment.status is ShipmentStatus.BOOKED
    assert structured(updated)["declared_qty"] == "1150"
    [current] = direct(lambda s: svc.get_documents(s, shipment_id))
    assert_parity(amended, current)
    assert (current.doc_type, current.version, current.qty) == (DocType.INVOICE, 2, Decimal("1200"))
    actors = direct(lambda s: set(s.scalars(select(AuditEvent.actor))))
    assert actors == {"document_verification_agent"}  # _meta.actor is the audit actor


async def test_domain_errors_come_back_typed(seeded: None, direct: Any) -> None:
    ids = direct(_ids)
    async with connect(mcp) as client:
        illegal = await client.call_tool(
            "update_status", {"shipment_id": str(ids["delayed"]), "status": "docked"}
        )
        uncleared = await client.call_tool(
            "update_status", {"shipment_id": str(ids["delayed"]), "status": "cleared"}
        )
        duplicate = await client.call_tool(
            "create_shipment",
            {"shipment": make_shipment(container_id=CLEAN).model_dump(mode="json")},
        )

    assert_tool_error(illegal, "InvalidStatusTransitionError")
    assert_tool_error(uncleared, "ClearanceRequiresApprovalError")
    assert_tool_error(duplicate, "DuplicateContainerError")


async def test_cancelled_shipment_is_refused_to_agents(direct: Any, session_factory: Any) -> None:
    with session_factory() as session, session.begin():
        quoted = svc.create_shipment(
            session,
            make_shipment(container_id="TGHU7008008", quote_date=date(2026, 9, 1),
                          quote_validity_days=7),
            "ops",
        )  # fmt: skip
        svc.update_status(session, quoted.id, ShipmentStatus.CANCELLED, "ops")

    async with connect(mcp) as client:
        result = await client.call_tool("get_active_shipment", {"shipment_id": str(quoted.id)})

    assert_tool_error(result, "ShipmentCancelledError")


async def test_clear_shipment_needs_an_approved_filing_decision(
    seeded: None, direct: Any, session_factory: Any
) -> None:
    ids = direct(_ids)
    with session_factory() as session, session.begin():
        for status in (ShipmentStatus.UNDER_REVIEW,):
            svc.update_status(session, ids["delayed"], status, "test")  # held -> under_review
        decision = approve_filing(session, ids["delayed"])
    async with connect(mcp) as client:
        wrong = await client.call_tool(
            "clear_shipment", {"shipment_id": str(ids["clean"]),
                               "filing_decision_id": str(decision.id)})  # fmt: skip
        cleared = await client.call_tool(
            "clear_shipment",
            {"shipment_id": str(ids["delayed"]), "filing_decision_id": str(decision.id)},
            meta={"actor": "supervisor"},
        )

    assert_tool_error(wrong, "InvalidFilingDecisionError")
    assert_parity(cleared, direct(lambda s: svc.get_shipment(s, ids["delayed"])))
    assert structured(cleared)["status"] == "cleared"
