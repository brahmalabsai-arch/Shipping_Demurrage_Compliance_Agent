"""Phase 12 — Mission Control: the case study end to end, the audit trail, the ROI view."""

from datetime import date

import pytest
from sqlalchemy.orm import Session, sessionmaker
from streamlit.testing.v1 import AppTest

from agents import graph
from agents.mcp_clients import memory_connector
from config.settings import PROJECT_ROOT
from scripts import seed_compliance, seed_fx_rates, seed_port_rates, seed_shipments
from services import roi_service

PAGE = str(PROJECT_ROOT / "dashboards" / "views" / "6_Mission_Control.py")
AS_OF = date(2026, 9, 28)


@pytest.fixture
def seeded(session_factory: sessionmaker[Session]) -> None:
    seed_shipments.seed()
    seed_port_rates.seed()
    seed_fx_rates.seed()
    seed_compliance.seed()


def _open_page() -> AppTest:
    at = AppTest.from_file(PAGE, default_timeout=120).run()
    assert not at.exception, at.exception
    return at


def test_empty_page_renders(session_factory: sessionmaker[Session]) -> None:
    at = _open_page()
    assert any("No snapshots yet" in i.value for i in at.info)
    assert any("No clearance runs yet" in i.value for i in at.info)


def test_roi_capture_and_compare_on_the_page(
    seeded: None, session_factory: sessionmaker[Session]
) -> None:
    at = _open_page()
    at.text_input(key="roi_name").set_value("fixtures")
    at.date_input(key="roi_as_of").set_value(AS_OF)
    at.button(key="roi_capture_submit").click()
    at.run()
    assert not at.error, [e.value for e in at.error]
    assert at.toast[0].value == "Captured ROI snapshot 'fixtures'."

    at.button(key="roi_compare").click()
    at.run()

    assert not at.exception, at.exception
    metrics = {m.label: m.value for m in at.metric}
    assert metrics["Demurrage avoided (INR)"] == "82,212.00"
    assert metrics["With the agents (INR)"] == "0.00"
    lines = at.table[-1].value
    assert dict(zip(lines["Container"], lines["Avoided (INR)"], strict=True))["MSCU7002002"] == (
        "23,868.00"
    )
    with session_factory() as session:
        assert roi_service.get_snapshot_by_name(session, "fixtures").as_of == AS_OF


def test_a_duplicate_snapshot_name_is_refused(
    seeded: None, session_factory: sessionmaker[Session]
) -> None:
    at = _open_page()
    for _ in range(2):
        at.text_input(key="roi_name").set_value("dup")
        at.button(key="roi_capture_submit").click()
        at.run()
    assert at.error[0].value == "An ROI snapshot named 'dup' exists"


def test_analyse_all_then_follow_one_run_in_the_audit_trail(
    seeded: None, session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(graph, "stdio_connector", memory_connector)
    at = _open_page()
    at.date_input(key="analysis_as_of").set_value(AS_OF)

    at.button(key="analyse_all").click()
    at.run()

    assert not at.exception, at.exception
    table = at.table[0].value
    assert set(table["Container"]) == {"MSKU7001001", "MSCU7002002", "CMAU7003003",
                                       "HLXU7004004"}  # fmt: skip
    rows = dict(zip(table["Container"], table["Papers"], strict=True))
    assert rows["MSCU7002002"] == "Quantities disagree" and rows["MSKU7001001"] == "In order"
    results = at.session_state["mc_analysis"]
    run_id = next(case.run_id for shipment, case in results
                  if shipment.container_id == "MSCU7002002")  # fmt: skip

    at.text_input(key="audit_run").set_value(run_id)
    at.run()

    trail = at.table[1].value  # [0] analysis, [1] audit trail (no clearance runs table)
    assert set(trail["What happened"]) == {"Papers checked", "Demurrage and risk assessed",
                                           "Rulebook consulted", "Checklist built"}  # fmt: skip
    assert set(trail["Run"]) == {run_id[:8]}
