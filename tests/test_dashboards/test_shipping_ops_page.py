"""Phase 1 exit criterion — the Shipping Ops dashboard round-trips a manual edit.

Drives the real Streamlit page headlessly (``AppTest``). The ``session_factory`` fixture points
``services.db.session_scope`` at the rolled-back test transaction, so the page and the
assertions read and write exactly the same data the service layer does.
"""

from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from streamlit.testing.v1 import AppTest

from agents import document_verification_agent, graph, supervisor
from agents.mcp_clients import memory_connector
from config.settings import PROJECT_ROOT
from models.db_models import AuditEvent
from models.enums import ContainerType, DocType, RunState, ShipmentStatus
from scripts import seed_compliance, seed_fx_rates, seed_port_rates, seed_shipments
from services import governance_service
from services import shipping_service as svc
from tests.fixtures.shipments import make_shipment

PAGE = str(PROJECT_ROOT / "dashboards" / "views" / "1_Shipping_Ops.py")
CID = "EDTU0000001"


def table_with(at: AppTest, column: str) -> Any:
    """The (last) table on the page that has this column."""
    return next(t.value for t in reversed(at.table) if column in t.value.columns)


def _open_page() -> AppTest:
    at = AppTest.from_file(PAGE, default_timeout=60).run()
    assert not at.exception, at.exception
    return at


def _seed(session_factory: sessionmaker[Session]) -> None:
    with session_factory() as session, session.begin():
        svc.create_shipment(session, make_shipment(container_id=CID), "fixture")


def test_manual_edit_round_trips_to_the_service_layer(
    session_factory: sessionmaker[Session],
) -> None:
    _seed(session_factory)
    at = _open_page()
    at.sidebar.text_input(key="actor").set_value("priya.ops")
    at.selectbox(key="selected_container").set_value(CID)

    at.number_input(key=f"edit_declared_qty_{CID}").set_value(1150.0)
    at.button(key=f"save_{CID}").click()
    at.run()

    assert not at.exception, at.exception
    assert not at.error, [e.value for e in at.error]
    assert "declared qty" in at.toast[0].value
    # Written through to the DB, audited with the dashboard operator as actor ...
    with session_factory() as session:
        stored = svc.get_shipment_by_container_id(session, CID)
        assert stored.declared_qty == Decimal("1150")
        event = session.scalars(
            select(AuditEvent).where(
                AuditEvent.entity_id == stored.id, AuditEvent.action == "shipment.updated"
            )
        ).one()
        assert event.actor == "priya.ops"
    # ... and read back into a fresh page load.
    reloaded = _open_page()
    reloaded.selectbox(key="selected_container").set_value(CID).run()
    assert reloaded.number_input(key=f"edit_declared_qty_{CID}").value == 1150.0


def test_status_control_offers_only_legal_moves_and_applies_them(
    session_factory: sessionmaker[Session],
) -> None:
    _seed(session_factory)
    at = _open_page()
    at.selectbox(key="selected_container").set_value(CID).run()

    picker = at.selectbox(key=f"next_status_{CID}")
    assert set(picker.options) == {"Booked", "Cancelled"}  # from 'quoted'
    picker.set_value(ShipmentStatus.BOOKED)
    at.button(key=f"apply_status_{CID}").click()
    at.run()

    assert not at.exception, at.exception
    with session_factory() as session:
        assert svc.get_shipment_by_container_id(session, CID).status is ShipmentStatus.BOOKED


def test_moving_a_stage_keeps_the_shipment_open(session_factory: sessionmaker[Session]) -> None:
    """The picker's labels must not change with the stage: Streamlit keys a select box by its
    labels, so a changed label reset the picker to the first shipment after every move."""
    seed_shipments.seed()
    at = _open_page()
    cid = "MSCU7002002"  # docked, not the first shipment in the list
    at.selectbox(key="selected_container").set_value(cid).run()

    at.selectbox(key=f"next_status_{cid}").set_value(ShipmentStatus.UNDER_REVIEW)
    at.button(key=f"apply_status_{cid}").click().run()

    assert not at.exception, at.exception
    assert at.toast[0].value == f"{cid} moved from docked to under review."
    assert at.session_state["selected_container"] == cid


def test_create_shipment_then_attach_document(session_factory: sessionmaker[Session]) -> None:
    at = _open_page()
    at.text_input(key="new_container").set_value("NEWU1111111")
    at.selectbox(key="new_status").set_value(ShipmentStatus.BOOKED)
    at.selectbox(key="new_container_type").set_value(ContainerType.HIGH_CUBE_40)
    at.text_input(key="new_cargo_type").set_value("Auto parts - radiators")
    at.text_input(key="new_hs_code").set_value("87089100")
    at.number_input(key="new_qty").set_value(300.0)
    at.number_input(key="new_value").set_value(45000.0)
    at.text_input(key="new_origin").set_value("CNSHA")
    at.date_input(key="new_etd_date").set_value(date(2026, 9, 1))
    at.button(key="create_submit").click()
    at.run()

    assert not at.exception, at.exception
    assert not at.error, [e.value for e in at.error]
    # the new shipment opens by itself, on the Shipments tab, whatever the filter was
    assert at.session_state["selected_container"] == "NEWU1111111"
    assert at.session_state["ops_tabs"] == "Shipments"
    assert at.toast[0].value == "Created NEWU1111111."

    at.selectbox(key="add_doc_type_NEWU1111111").set_value(DocType.PACKING_LIST)
    at.number_input(key="add_doc_qty_NEWU1111111").set_value(300.0)
    at.button(key="add_doc_submit_NEWU1111111").click()
    at.run()

    assert not at.exception, at.exception
    with session_factory() as session:
        created = svc.get_shipment_by_container_id(session, "NEWU1111111")
        assert (created.status, created.declared_value) == (ShipmentStatus.BOOKED, Decimal("45000"))
        assert created.container_type is ContainerType.HIGH_CUBE_40
        [doc] = svc.get_documents(session, created.id)
        assert (doc.doc_type, doc.qty, doc.currency) == (DocType.PACKING_LIST, Decimal("300"), None)


def test_amend_any_document_not_just_the_first(session_factory: sessionmaker[Session]) -> None:
    """Pick the packing list (not the first document listed) and amend it: the fields follow
    the pick, so the new quantity is what gets issued."""
    seed_shipments.seed()
    at = _open_page()
    cid = "MSCU7002002"  # invoice 800, packing list 760
    at.selectbox(key="selected_container").set_value(cid).run()
    with session_factory() as session:
        docs = svc.get_documents(session, svc.get_shipment_by_container_id(session, cid).id)
    packing = next(d for d in docs if d.doc_type is DocType.PACKING_LIST)
    label = next(o for o in at.selectbox(key=f"amend_pick_{cid}").options
                 if o.startswith("Packing list"))  # fmt: skip

    at.selectbox(key=f"amend_pick_{cid}").set_value(label).run()
    assert at.number_input(key=f"amend_qty_{cid}_{packing.id}").value == 760.0
    at.number_input(key=f"amend_qty_{cid}_{packing.id}").set_value(800.0)
    at.button(key=f"amend_submit_{cid}").click().run()

    assert not at.exception, at.exception
    assert at.toast[0].value == f"Issued version 2 of the packing list for {cid}."
    with session_factory() as session:
        current = svc.get_documents(session, packing.shipment_id)
    assert {(d.doc_type, d.qty) for d in current} == {
        (DocType.INVOICE, Decimal("800")), (DocType.PACKING_LIST, Decimal("800")),
        (DocType.BILL_OF_LADING, Decimal("800")),
    }  # fmt: skip


# --------------------------------------------------------------------------- Phase 7


def _verify_on_page(monkeypatch: pytest.MonkeyPatch, container_id: str) -> AppTest:
    """Open the page on a seeded shipment and press "Check papers" (MCP in memory)."""
    monkeypatch.setattr(document_verification_agent, "stdio_connector", memory_connector)
    seed_shipments.seed()
    seed_fx_rates.seed()
    at = _open_page()
    at.selectbox(key="selected_container").set_value(container_id).run()
    at.button(key=f"verify_{container_id}").click()
    at.run()
    assert not at.exception, at.exception
    assert not at.error, [e.value for e in at.error]
    return at


def test_verify_documents_shows_the_field_level_discrepancy(
    session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    at = _verify_on_page(monkeypatch, "MSCU7002002")

    [warning] = at.warning
    assert "MSCU7002002: 1 discrepancy(ies) to resolve" in warning.value
    discrepancies = table_with(at, "Problem")
    assert list(discrepancies["Problem"]) == ["Quantities disagree"]
    assert list(discrepancies["Difference"]) == ["40"]
    rates = table_with(at, "How it was chosen")
    assert list(rates["Stage"]) == ["Quotation", "Bill of lading", "Commercial invoice"]
    with session_factory() as session:
        run = session.scalars(
            select(AuditEvent).where(AuditEvent.action == "document_verification.completed")
        ).one()
    assert run.actor == document_verification_agent.AGENT


def test_verify_documents_on_a_clean_shipment(
    session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    at = _verify_on_page(monkeypatch, "MSKU7001001")

    assert not at.warning
    assert "nothing to resolve" in at.success[0].value


def test_container_type_is_editable_on_the_page(session_factory: sessionmaker[Session]) -> None:
    _seed(session_factory)
    at = _open_page()
    at.selectbox(key="selected_container").set_value(CID).run()
    assert at.table[0].value.loc[0, "Type"] == "20ft"

    at.selectbox(key=f"edit_container_type_{CID}").set_value(ContainerType.DRY_40)
    at.button(key=f"save_{CID}").click()
    at.run()

    assert not at.exception, at.exception
    assert not at.error, [e.value for e in at.error]
    with session_factory() as session:
        stored = svc.get_shipment_by_container_id(session, CID)
    assert stored.container_type is ContainerType.DRY_40


# --------------------------------------------------------------------------- Phase 8


def test_assess_demurrage_risk_shows_the_estimate_and_risk(
    session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The delayed 40hc shipment: accruing and held → high risk, with the priced stay."""
    monkeypatch.setattr(graph, "stdio_connector", memory_connector)
    monkeypatch.setattr(graph, "today_ist", lambda: date(2026, 9, 28))
    seed_shipments.seed()
    seed_port_rates.seed()
    seed_fx_rates.seed()
    at = _open_page()
    at.selectbox(key="selected_container").set_value("HLXU7004004").run()

    at.button(key="assess_HLXU7004004").click()
    at.run()

    assert not at.exception, at.exception
    assert not at.error, [e.value for e in at.error]
    [warning] = at.warning
    assert warning.value.startswith("High risk as of 28 Sep 2026.")
    metrics = {m.label: m.value for m in at.metric}
    assert metrics["Days charged"] == "16"
    assert metrics["Demurrage (USD)"] == "1,600.00"
    assert metrics["Demurrage (INR)"] == "141,440.00"
    # Phase 10: documents are clean, so the checklist is ready despite the (urgent) demurrage
    [ready] = at.success
    assert ready.value == "Ready to file: 0 must-fix, 1 to do, 5 notes."
    items = table_with(at, "Priority")
    assert "Demurrage: high risk" in set(items["What"])


def test_analysis_checklist_lists_the_blocking_discrepancy_with_guidance(
    session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(graph, "stdio_connector", memory_connector)
    monkeypatch.setattr(graph, "today_ist", lambda: date(2026, 9, 28))
    seed_shipments.seed()
    seed_port_rates.seed()
    seed_fx_rates.seed()
    seed_compliance.seed()
    at = _open_page()
    at.selectbox(key="selected_container").set_value("MSCU7002002").run()

    at.button(key="assess_MSCU7002002").click()
    at.run()

    assert not at.exception, at.exception
    assert not at.error, [e.value for e in at.error]
    assert any(i.value.startswith("Not ready to file yet: 1 must-fix") for i in at.info)
    items = table_with(at, "Priority")
    first = items.iloc[0]
    assert (first["Priority"], first["What"]) == ("Must fix", "Quantities disagree")
    assert first["Found by"] == "Document check"
    assert "DOC-quantity-consistency v1" in first["Rules and data used"]


# --------------------------------------------------------------------------- Phase 11


def test_cleared_is_never_a_manual_move(session_factory: sessionmaker[Session]) -> None:
    with session_factory() as session, session.begin():
        created = svc.create_shipment(session, make_shipment(container_id=CID), "fixture")
        for status in (ShipmentStatus.BOOKED, ShipmentStatus.IN_TRANSIT, ShipmentStatus.DOCKED,
                       ShipmentStatus.UNDER_REVIEW):  # fmt: skip
            svc.update_status(session, created.id, status, "fixture")
    at = _open_page()
    at.selectbox(key="selected_container").set_value(CID).run()

    assert at.selectbox(key=f"next_status_{CID}").options == ["Held by customs"]
    assert any("reached only by filing" in c.value for c in at.caption)


def test_clearance_run_from_start_to_filed(
    session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch, checkpoints: None
) -> None:
    monkeypatch.setattr(supervisor, "stdio_connector", memory_connector)
    seed_shipments.seed()
    seed_port_rates.seed()
    seed_fx_rates.seed()
    seed_compliance.seed()
    at = _open_page()
    at.selectbox(key="selected_container").set_value("MSKU7001001").run()
    at.date_input(key="clearance_as_of_MSKU7001001").set_value(date(2026, 9, 28))

    at.button(key="clearance_start_MSKU7001001").click()
    at.run()
    assert not at.exception, at.exception
    assert not at.error, [e.value for e in at.error]
    assert any(s.value.startswith("The checklist is ready.") for s in at.success)
    # no reviewer chosen yet: nobody can decide
    assert any(w.value.startswith("Choose yourself") for w in at.warning)
    assert not [b for b in at.button if b.key and b.key.startswith("approve_filing_")]

    at.selectbox(key="reviewer").set_value("Asha Rao").run()
    approve = next(b for b in at.button if b.key and b.key.startswith("approve_filing_"))
    approve.click()
    at.run()

    assert not at.exception, at.exception
    assert not at.error, [e.value for e in at.error]
    assert at.toast[0].value.startswith("Filed and cleared.")
    with session_factory() as session:
        shipment = svc.get_shipment_by_container_id(session, "MSKU7001001")
        assert shipment.status is ShipmentStatus.CLEARED
        [run] = governance_service.list_runs(session, shipment.id)
        assert run.state is RunState.FILED
