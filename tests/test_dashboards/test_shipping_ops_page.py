"""Phase 1 exit criterion — the Shipping Ops dashboard round-trips a manual edit.

Drives the real Streamlit page headlessly (``AppTest``). The ``session_factory`` fixture points
``services.db.session_scope`` at the rolled-back test transaction, so the page and the
assertions read and write exactly the same data the service layer does.
"""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from streamlit.testing.v1 import AppTest

from agents import document_verification_agent
from agents.mcp_clients import memory_connector
from config.settings import PROJECT_ROOT
from models.db_models import AuditEvent
from models.enums import ContainerType, DocType, ShipmentStatus
from scripts import seed_fx_rates, seed_shipments
from services import shipping_service as svc
from tests.fixtures.shipments import make_shipment

PAGE = str(PROJECT_ROOT / "dashboards" / "pages" / "1_Shipping_Ops.py")
CID = "EDTU0000001"


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
    assert "declared_qty" in at.success[0].value
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
    assert set(picker.options) == {"booked", "cancelled"}  # from 'quoted'
    picker.set_value(ShipmentStatus.BOOKED)
    at.button(key=f"apply_status_{CID}").click()
    at.run()

    assert not at.exception, at.exception
    with session_factory() as session:
        assert svc.get_shipment_by_container_id(session, CID).status is ShipmentStatus.BOOKED


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

    at.selectbox(key="selected_container").set_value("NEWU1111111").run()
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


# --------------------------------------------------------------------------- Phase 7


def _verify_on_page(monkeypatch: pytest.MonkeyPatch, container_id: str) -> AppTest:
    """Open the page on a seeded shipment and press "Verify documents" (MCP in memory)."""
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
    discrepancies = at.dataframe[2].value
    assert list(discrepancies["Finding"]) == ["quantity_mismatch"]
    assert list(discrepancies["Delta"]) == ["40"]
    rates = at.dataframe[3].value
    assert list(rates["Stage"]) == ["quotation", "bol", "invoice (commercial)"]
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
    assert at.dataframe[0].value.loc[0, "Type"] == "20ft"

    at.selectbox(key=f"edit_container_type_{CID}").set_value(ContainerType.DRY_40)
    at.button(key=f"save_{CID}").click()
    at.run()

    assert not at.exception, at.exception
    assert not at.error, [e.value for e in at.error]
    with session_factory() as session:
        stored = svc.get_shipment_by_container_id(session, CID)
    assert stored.container_type is ContainerType.DRY_40
