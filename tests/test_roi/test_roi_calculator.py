"""Phase 12 — the ROI calculator: exit criterion and ``edge_cases.md`` Phase 12.

Exit criterion: "before" (agents disabled, every fixture issue manifests at the port) vs
"after" (agents enabled, issues caught pre-dock) over the same 4 seeded shipments gives a
number that can be rebuilt by hand from the tariff (Demurrage page) and gazetted FX (FX Rates
page) figures. The hand calculation below is independent of the calculator's code.
"""

from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy.orm import Session, sessionmaker

from models.enums import ContainerType, FxStage, RuleDecision, ShipmentStatus
from models.schemas import (
    DocumentAmend,
    PortRateUpsert,
    RoiAssumptions,
    RuleProposal,
)
from roi.calculator import RoiLine, compare
from scripts import seed_compliance, seed_fx_rates, seed_port_rates, seed_shipments
from services import (
    compliance_service,
    demurrage_service,
    fx_service,
    roi_service,
    shipping_service,
)
from services.errors import InvalidAssumptionsError
from tests.fixtures.shipments import make_shipment

AS_OF = date(2026, 9, 28)

# ---------------------------------------------------------------- the hand calculation
# Default assumptions: a box with its papers in order needs 3 days in port; without the agents,
# customs holds it 5 more days for a quantity mismatch, 7 for a missing document (the longest
# hold counts). Tariffs: the period in force on the arrival day (INNSA, USD/container/day);
# FX: gazetted USD/INR in force on 2026-09-28 = 88.40 (the 09-21..09-30 revision).
#
# MSKU7001001  20ft  arr 09-20  5 free @ 45.00   no issues   before 3d -> 0   after 3d -> 0
# MSCU7002002  40ft  arr 09-22  5 free @ 90.00   qty 800/760 before 3+5 = 8d -> 3 x 90 = 270.00
#                                                            x 88.40 = 23,868.00; after 3d -> 0
# CMAU7003003  40ft  arr 10-05  4 free @ 110.00  no invoice  before 3+7 = 10d -> 6 x 110 = 660.00
#              (October tariff)                              x 88.40 = 58,344.00; after 3d -> 0
# HLXU7004004  40hc  arr 09-08  5 free @ 100.00  no issues   before 3d -> 0   after 3d -> 0
#
# avoided = 23,868.00 + 58,344.00 = 82,212.00 INR
EXPECTED_AVOIDED = {
    "MSKU7001001": Decimal("0.00"),
    "MSCU7002002": Decimal("23868.00"),
    "CMAU7003003": Decimal("58344.00"),
    "HLXU7004004": Decimal("0.00"),
}
EXPECTED_TOTAL = Decimal("82212.00")


@pytest.fixture
def seeded(session_factory: sessionmaker[Session]) -> None:
    seed_shipments.seed()
    seed_port_rates.seed()
    seed_fx_rates.seed()
    seed_compliance.seed()


def capture(session_factory: sessionmaker[Session], name: str, **assumptions: Any) -> Any:
    with session_factory() as session, session.begin():
        return roi_service.capture_snapshot(
            session, name, AS_OF, RoiAssumptions(**assumptions), "test"
        )


def by_container(lines: list[RoiLine]) -> dict[str, RoiLine]:
    return {line.container_id: line for line in lines}


# --------------------------------------------------------------------------- exit criterion


def test_before_after_over_the_four_seeded_shipments(
    seeded: None, session_factory: sessionmaker[Session]
) -> None:
    snapshot = capture(session_factory, "fixtures")

    result = compare(snapshot, snapshot)

    lines = by_container(result.lines)
    assert set(lines) == set(EXPECTED_AVOIDED)
    assert {cid: line.avoided_inr for cid, line in lines.items()} == EXPECTED_AVOIDED
    assert result.total_avoided_inr == EXPECTED_TOTAL
    assert (result.total_before_inr, result.total_after_inr) == (EXPECTED_TOTAL, Decimal("0.00"))
    assert result.excluded == []
    assert lines["MSCU7002002"].issues == ["quantity_mismatch"]
    assert lines["CMAU7003003"].issues == ["missing_document"]
    assert lines["MSCU7002002"].caught == ["quantity_mismatch"]  # the agents catch the same


def test_every_line_rebuilds_from_the_dashboard_figures(
    seeded: None, session_factory: sessionmaker[Session]
) -> None:
    """Conventions §9: days avoided x daily rate x FX rate — from the figures the Demurrage and
    FX Rates pages show (the same service reads)."""
    result = compare(snapshot := capture(session_factory, "fixtures"), snapshot)

    with session_factory() as session:
        for line in result.lines:
            shipment = shipping_service.get_shipment(session, line.shipment_id)
            item = next(s for s in snapshot.content.shipments if s.shipment.id == line.shipment_id)
            assert item.arrival_date is not None and line.before and line.after
            tariff = demurrage_service.get_rate(
                session, shipment.destination_port, shipment.container_type, item.arrival_date
            )
            fx = fx_service.resolve_rate(session, "USD/INR", AS_OF, FxStage.PORT_CHARGES)
            assert (line.before.tariff_id, line.before.daily_rate) == (tariff.id, tariff.daily_rate)
            assert (line.before.fx_source_id, line.before.fx_rate) == (fx.source_id, fx.rate)
            rebuilt = (line.chargeable_days_avoided * tariff.daily_rate * fx.rate).quantize(
                Decimal("0.01")
            )
            assert line.avoided_inr == rebuilt
            assert line.before.basis.endswith(f"INR {line.before.amount_inr}")


# --------------------------------------------------------------------------- edge cases


def test_a_shipment_without_issues_adds_nothing_in_either_run(
    seeded: None, session_factory: sessionmaker[Session]
) -> None:
    """Edge case: the clean shipment contributes zero in both runs — whatever the assumptions."""
    for name, assumptions in (("default", {}),
                              ("slow", {"base_clearance_days": 9, "combine": "sum"})):  # fmt: skip
        result = compare(snapshot := capture(session_factory, name, **assumptions), snapshot)
        clean = by_container(result.lines)["MSKU7001001"]
        assert clean.issues == [] and clean.avoided_inr == Decimal("0.00")
        assert clean.before is not None and clean.after is not None
        assert clean.before.amount_inr == clean.after.amount_inr
        if name == "slow":  # 9 days > 5 free: it costs money, but the same in both scenarios
            assert clean.before.chargeable_days == 4


def test_a_comparison_in_flight_ignores_live_edits(
    seeded: None, session_factory: sessionmaker[Session]
) -> None:
    """Edge case: inputs are frozen in the snapshot passed in. Edit the live tariffs, FX,
    documents and rulebook after the first line is computed — the result is unchanged."""
    snapshot = capture(session_factory, "frozen")
    untouched = compare(snapshot, snapshot)
    edited = False

    def edit_live_data(line: RoiLine) -> None:
        nonlocal edited
        if edited:
            return
        edited = True
        with session_factory() as session, session.begin():
            demurrage_service.upsert_rate(session, PortRateUpsert(
                port_code="INNSA", container_type=ContainerType.DRY_40, currency="USD",
                free_days=1, daily_rate=Decimal("999.00"), effective_from=date(2026, 1, 1)),
                "ops")  # fmt: skip
            mscu = shipping_service.get_shipment_by_container_id(session, "MSCU7002002")
            packing = next(d for d in shipping_service.get_documents(session, mscu.id)
                           if d.doc_type.value == "packing_list")  # fmt: skip
            shipping_service.amend_document(session, packing.id,
                                            DocumentAmend(qty=Decimal(800)), "ops")  # fmt: skip
            rule = compliance_service.propose_change(session, RuleProposal(
                rule_key="TEST-new", content="x", hs_codes=[],
                effective_date=AS_OF), "ops")  # fmt: skip
            compliance_service.decide(session, rule.id, RuleDecision.APPROVE, "Asha Rao")

    in_flight = compare(snapshot, snapshot, on_line=edit_live_data)

    assert edited
    assert in_flight == untouched
    with session_factory() as session:  # the stored snapshot is unchanged too
        assert roi_service.get_snapshot(session, snapshot.id) == snapshot
    # ...while a snapshot taken now does see the edits (so they would have mattered)
    later = compare(after := capture(session_factory, "after edits"), after)
    assert by_container(later.lines)["MSCU7002002"].issues == []
    assert later.total_avoided_inr != untouched.total_avoided_inr


def test_two_snapshots_compare_two_edited_states(
    seeded: None, session_factory: sessionmaker[Session]
) -> None:
    """Architecture §9: before and after may be two different edited states — here the
    October 40ft tariff rises between them; each scenario is priced with its own snapshot."""
    before = capture(session_factory, "september")
    with session_factory() as session, session.begin():
        demurrage_service.upsert_rate(session, PortRateUpsert(
            port_code="INNSA", container_type=ContainerType.DRY_40, currency="USD", free_days=4,
            daily_rate=Decimal("150.00"), effective_from=date(2026, 10, 1)), "ops")  # fmt: skip
    after = capture(session_factory, "october rise")

    result = compare(before, after)

    cmau = by_container(result.lines)["CMAU7003003"]
    assert cmau.before is not None and cmau.after is not None
    assert (cmau.before.daily_rate, cmau.after.daily_rate) == (Decimal("110.00"),
                                                              Decimal("150.00"))  # fmt: skip
    assert cmau.avoided_inr == Decimal("58344.00")  # after: 3 days, 0 chargeable at either rate
    assert (result.before_snapshot.name, result.after_snapshot.name) == ("september",
                                                                         "october rise")  # fmt: skip


def test_a_shipment_that_cannot_be_priced_is_excluded_not_guessed(
    seeded: None, session_factory: sessionmaker[Session]
) -> None:
    with session_factory() as session, session.begin():  # INNSA has no reefer tariff
        shipping_service.create_shipment(
            session, make_shipment(container_type=ContainerType.REEFER_20,
                                   status=ShipmentStatus.BOOKED), "test")  # fmt: skip
    result = compare(snapshot := capture(session_factory, "reefer"), snapshot)

    reefer = by_container(result.lines)["TSTU0000001"]
    assert reefer.excluded is not None and reefer.excluded.startswith("tariff: ")
    assert reefer.avoided_inr == Decimal("0.00") and reefer.before is None
    assert result.total_avoided_inr == EXPECTED_TOTAL  # the others still priced
    assert result.excluded == [f"TSTU0000001: {reefer.excluded}"]


def test_the_issues_hold_rule_is_an_explicit_assumption(
    seeded: None, session_factory: sessionmaker[Session]
) -> None:
    result = compare(snapshot := capture(session_factory, "longer holds",
                                         hold_days={"missing_document": 10, "quantity_mismatch": 1,
                                                    "value_mismatch": 5, "currency_mismatch": 4,
                                                    "invalid_currency": 4, "date_mismatch": 2,
                                                    "quote_expired": 2,
                                                    "fx_rate_unavailable": 2}),
                     snapshot)  # fmt: skip
    lines = by_container(result.lines)
    assert lines["MSCU7002002"].avoided_inr == Decimal("0.00")  # 3 + 1 = 4 days < 5 free
    # 3 + 10 = 13 days, 4 free -> 9 x 110.00 = 990.00 x 88.40 = 87,516.00
    assert lines["CMAU7003003"].avoided_inr == Decimal("87516.00")
    assert result.assumptions_before.hold_days["missing_document"] == 10
    with pytest.raises(InvalidAssumptionsError):
        capture(session_factory, "incomplete", hold_days={"missing_document": 7})
