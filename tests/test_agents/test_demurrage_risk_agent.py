"""Phase 8 — the Demurrage & Risk agent over MCP (in-memory servers, real protocol) against the
seeded JNPT shipments, tariffs and FX rates, alone and after document verification.

Exit criterion: the already-delayed fixture's estimate equals an independent hand calculation
to the cent. Conventions (user decisions): the arrival day is day 1 in port; the tariff in force
on the arrival day prices the stay; USD tariffs convert at the USD/INR gazetted rate in force
on the analysis date; money is rounded half-up to 0.01 once, at the end.
"""

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from agents import demurrage_risk_agent as agent
from agents import graph
from agents.mcp_clients import memory_connector
from agents.state import CaseState
from models.db_models import AuditEvent
from models.enums import ContainerType, FxStage, ShipmentStatus
from models.schemas import PortRateUpsert
from scripts import seed_fx_rates, seed_port_rates, seed_shipments
from services import demurrage_service, fx_service, shipping_service
from services.errors import ShipmentCancelledError
from tests.fixtures.governance import clear
from tests.fixtures.shipments import make_shipment

S = ShipmentStatus
AS_OF = date(2026, 9, 28)
TO_DOCKED = (S.BOOKED, S.IN_TRANSIT, S.DOCKED)

# ---- The hand calculation (independent of the agent) ------------------------------------
# HLXU7004004: 40ft high-cube, destination INNSA, ETA 2026-09-08 05:00 UTC = 10:30 IST on
# 09-08 (docked, then held). INNSA 40hc tariff in force on 09-08: 5 free days, USD 100.00/day.
# In port 09-08 .. 09-28 with the arrival day counted: 21 days. Chargeable: 21 - 5 = 16
# (from 09-13). 16 x 100.00 = USD 1,600.00.
# USD/INR gazetted on 09-28: 09-16..09-30 = 88.30, revised for 09-21..09-30 to 88.40 → 88.40.
# 1,600.00 x 88.40 = INR 141,440.00.
HAND_DAYS, HAND_CHARGEABLE = 21, 16
HAND_USD = Decimal("1600.00")
HAND_FX = Decimal("88.40")
HAND_INR = Decimal("141440.00")


@pytest.fixture
def seeded(session_factory: sessionmaker[Session]) -> None:
    seed_shipments.seed()
    seed_port_rates.seed()
    seed_fx_rates.seed()


async def assess(shipment_id: UUID, as_of: date = AS_OF) -> CaseState:
    return await agent.assess_shipment(shipment_id, connector=memory_connector, as_of=as_of)


async def analyse(shipment_id: UUID, as_of: date = AS_OF) -> CaseState:
    return await graph.analyse_shipment(
        shipment_id, connector=memory_connector, model_factory=lambda: None, as_of=as_of
    )


def shipment_id(session_factory: sessionmaker[Session], container_id: str) -> UUID:
    with session_factory() as session:
        return shipping_service.get_shipment_by_container_id(session, container_id).id


def create(
    session_factory: sessionmaker[Session],
    status_path: tuple[ShipmentStatus, ...] = TO_DOCKED,
    **overrides: Any,
) -> UUID:
    """An ad-hoc shipment (the clean fixture unless overridden), walked through its statuses."""
    with session_factory() as session, session.begin():
        record = shipping_service.create_shipment(session, make_shipment(**overrides), "test")
        for status in status_path:
            if status is ShipmentStatus.CLEARED:  # only by filing with an approval (Phase 11)
                clear(session, record.id)
            else:
                shipping_service.update_status(session, record.id, status, "test")
    return record.id


def tool_calls(logs: list[Any]) -> list[str]:
    return [e["tool"] for e in logs if e["event"] == "agent.tool_call"]


# --------------------------------------------------------------------------- exit criterion


async def test_delayed_shipment_estimate_matches_the_hand_calculation_to_the_cent(
    seeded: None, session_factory: sessionmaker[Session]
) -> None:
    sid = shipment_id(session_factory, "HLXU7004004")

    state = await assess(sid)

    e = state.demurrage_estimate
    assert e is not None
    assert (e.days_in_port, e.chargeable_days) == (HAND_DAYS, HAND_CHARGEABLE)
    assert (e.amount, e.currency) == (HAND_USD, "USD")
    assert e.fx is not None and e.fx.rate == HAND_FX
    assert e.amount_inr == HAND_INR
    assert str(e.amount_inr) == "141440.00"  # to the cent, not merely numerically equal
    # every figure is traceable to the rows it came from
    with session_factory() as session:
        tariff = demurrage_service.get_rate(session, "INNSA", ContainerType.HIGH_CUBE_40,
                                            date(2026, 9, 8))  # fmt: skip
        gazetted = fx_service.get_gazetted_rate(session, "USD/INR", AS_OF)
    assert e.tariff_id == tariff.id and (e.free_days, e.daily_rate) == (5, Decimal("100.00"))
    assert e.fx.stage is FxStage.PORT_CHARGES and e.fx.source_id == gazetted.id
    assert gazetted.revision_of is not None  # the 09-21 correction, not the original 88.30
    assert (e.arrival_date, e.charges_from, e.as_of) == (
        date(2026, 9, 8), date(2026, 9, 13), AS_OF,
    )  # fmt: skip
    assert e.basis == (
        "INNSA 40hc tariff in force on 2026-09-08: 5 free day(s), then 100.00 USD/day. 21 day(s) "
        "in port (2026-09-08 to 2026-09-28, arrival day counted) - 5 free = 16 chargeable x "
        "100.00 = 1600.00 USD x USD/INR 88.400000 (gazetted, 2026-09-28) = INR 141440.00."
    )
    assert state.risk == "high" and state.data_gaps == []
    assert any("held by customs" in r for r in state.risk_reasons)


# --------------------------------------------------------------------------- edge cases, Phase 8


async def test_five_days_past_free_days_costs_five_daily_rates(
    seeded: None, session_factory: sessionmaker[Session]
) -> None:
    """Edge case: 5 days past free days → 5 x daily_rate at the gazetted rate on the analysis
    date. 09-08..09-17 = 10 days, 5 free → 5 x 100.00 = USD 500.00; USD/INR on 09-17 is the
    original 88.30 (the correction starts 09-21) → INR 44,150.00."""
    state = await assess(shipment_id(session_factory, "HLXU7004004"), as_of=date(2026, 9, 17))

    e = state.demurrage_estimate
    assert e is not None and e.chargeable_days == 5
    assert (e.amount, e.amount_inr) == (Decimal("500.00"), Decimal("44150.00"))
    assert e.fx is not None and e.fx.rate == Decimal("88.30")


async def test_document_mismatch_and_missing_fx_give_a_partial_result_with_data_gaps(
    seeded: None, session_factory: sessionmaker[Session]
) -> None:
    """Edge case: documents flagged AND no gazetted rate for the analysis date (after 10-31) →
    the USD estimate is kept, the INR figure is missing and named in data_gaps; no raise."""
    as_of = date(2026, 11, 5)

    state = await analyse(shipment_id(session_factory, "MSCU7002002"), as_of=as_of)

    assert [d.code for d in state.document_discrepancies] == ["quantity_mismatch"]
    e = state.demurrage_estimate
    assert e is not None
    # 09-22..11-05 = 45 days, 5 free (tariff on 09-22) → 40 x 90.00 = USD 3,600.00
    assert (e.days_in_port, e.chargeable_days, e.amount) == (45, 40, Decimal("3600.00"))
    assert (e.fx, e.amount_inr, e.daily_rate_inr) == (None, None, None)
    [gap] = state.data_gaps
    assert gap.startswith("fx_rate: no gazetted USD/INR rate for 2026-11-05")
    assert state.risk == "high"
    assert any("1 document discrepancy(ies) (quantity_mismatch)" in r for r in state.risk_reasons)


# --------------------------------------------------------------------------- edge cases, Phases 1-2


async def test_unknown_eta_is_indeterminate_not_zero_risk(
    seeded: None, session_factory: sessionmaker[Session]
) -> None:
    sid = create(session_factory, status_path=(S.BOOKED, S.IN_TRANSIT), eta=None)

    with structlog.testing.capture_logs() as logs:
        state = await assess(sid)

    assert state.risk == "indeterminate"
    assert state.demurrage_estimate is None
    assert state.data_gaps == ["eta: not set on the shipment record, so the arrival day is unknown"]
    assert tool_calls(logs) == ["get_active_shipment"]  # nothing priced on a guessed date


async def test_zero_free_days_charges_from_the_arrival_day(
    seeded: None, session_factory: sessionmaker[Session]
) -> None:
    with session_factory() as session, session.begin():
        demurrage_service.upsert_rate(session, PortRateUpsert(
            port_code="INPAV", container_type=ContainerType.DRY_20, currency="USD", free_days=0,
            daily_rate=Decimal("50.00"), effective_from=date(2026, 1, 1)), "test")  # fmt: skip
    sid = create(session_factory, destination_port="INPAV",
                 eta=datetime(2026, 9, 26, 6, tzinfo=UTC))  # fmt: skip

    state = await assess(sid, as_of=date(2026, 9, 26))

    e = state.demurrage_estimate
    assert e is not None
    assert (e.days_in_port, e.chargeable_days, e.charges_from) == (1, 1, date(2026, 9, 26))
    assert (e.amount, e.amount_inr) == (Decimal("50.00"), Decimal("4420.00"))  # x 88.40
    assert state.risk == "high"


async def test_tariff_revised_after_docking_does_not_reprice_the_stay(
    seeded: None, session_factory: sessionmaker[Session]
) -> None:
    """Edge case: a new INNSA 40hc period from 09-15 (after the 09-08 arrival) — the stay is
    still priced at the tariff in force on arrival."""
    with session_factory() as session, session.begin():
        demurrage_service.upsert_rate(session, PortRateUpsert(
            port_code="INNSA", container_type=ContainerType.HIGH_CUBE_40, currency="USD",
            free_days=2, daily_rate=Decimal("500.00"), effective_from=date(2026, 9, 15)),
            "test")  # fmt: skip

    state = await assess(shipment_id(session_factory, "HLXU7004004"))

    e = state.demurrage_estimate
    assert e is not None
    assert (e.free_days, e.daily_rate, e.amount_inr) == (5, Decimal("100.00"), HAND_INR)


# --------------------------------------------------------------------------- the other fixtures


async def test_every_fixture_on_the_analysis_date(
    seeded: None, session_factory: sessionmaker[Session]
) -> None:
    """Each docked shipment is priced on its own tariff row (20ft / 40ft / 40hc)."""
    states = {
        cid: await assess(shipment_id(session_factory, cid))
        for cid in ("MSKU7001001", "MSCU7002002", "CMAU7003003", "HLXU7004004")
    }

    def figures(cid: str) -> tuple[Any, ...]:
        e = states[cid].demurrage_estimate
        assert e is not None
        return (e.container_type, e.days_in_port, e.chargeable_days, e.amount, e.amount_inr)

    # clean: 20ft, 09-20..09-28 = 9 days, 5 free → 4 x 45.00 = 180.00 USD x 88.40
    assert figures("MSKU7001001") == (ContainerType.DRY_20, 9, 4, Decimal("180.00"),
                                      Decimal("15912.00"))  # fmt: skip
    # qty mismatch: 40ft, 09-22..09-28 = 7 days, 5 free → 2 x 90.00 = 180.00 USD x 88.40
    assert figures("MSCU7002002") == (ContainerType.DRY_40, 7, 2, Decimal("180.00"),
                                      Decimal("15912.00"))  # fmt: skip
    # at sea until 10-05: nothing accrued; October 40ft tariff (4 free) → charges from 10-09
    assert figures("CMAU7003003") == (ContainerType.DRY_40, 0, 0, Decimal("0.00"),
                                      Decimal("0.00"))  # fmt: skip
    at_sea = states["CMAU7003003"]
    assert at_sea.demurrage_estimate is not None
    assert at_sea.demurrage_estimate.charges_from == date(2026, 10, 9)
    assert at_sea.risk == "low"
    assert figures("HLXU7004004")[3:] == (HAND_USD, HAND_INR)


@pytest.mark.parametrize(
    ("as_of", "risk", "reason"),
    [
        (date(2026, 9, 23), "low", "in free time until 2026-09-24"),
        (date(2026, 9, 24), "medium", "free time ends 2026-09-24; charges start 2026-09-25"),
        (date(2026, 9, 25), "high", "demurrage accruing since 2026-09-25"),
    ],
)
async def test_risk_rises_as_free_time_runs_out(
    seeded: None, session_factory: sessionmaker[Session], as_of: date, risk: str, reason: str
) -> None:
    """Clean shipment, 20ft arrived 09-20 with 5 free days (09-20..09-24)."""
    state = await assess(shipment_id(session_factory, "MSKU7001001"), as_of=as_of)

    assert state.risk == risk
    assert state.risk_reasons[0].startswith(reason)


async def test_eta_passed_without_docking_is_medium_risk(
    seeded: None, session_factory: sessionmaker[Session]
) -> None:
    state = await assess(shipment_id(session_factory, "CMAU7003003"), as_of=date(2026, 10, 7))

    assert state.risk == "medium"
    assert state.risk_reasons == [
        "ETA 2026-10-05 has passed but the shipment is not docked (status in_transit)"
    ]


async def test_cleared_shipment_has_no_risk(
    seeded: None, session_factory: sessionmaker[Session]
) -> None:
    sid = create(session_factory, status_path=(*TO_DOCKED, S.UNDER_REVIEW, S.CLEARED))

    state = await assess(sid)

    assert (state.risk, state.demurrage_estimate, state.data_gaps) == ("none", None, [])


async def test_no_tariff_for_the_container_type_is_indeterminate(
    seeded: None, session_factory: sessionmaker[Session]
) -> None:
    sid = create(session_factory, container_type=ContainerType.REEFER_20)

    state = await assess(sid)

    assert state.risk == "indeterminate" and state.demurrage_estimate is None
    [gap] = state.data_gaps
    assert gap.startswith("tariff: ") and "20rf" in gap


async def test_cancelled_shipment_stops_the_agent(
    seeded: None, session_factory: sessionmaker[Session]
) -> None:
    sid = create(session_factory, status_path=(S.BOOKED, S.CANCELLED))

    with pytest.raises(ShipmentCancelledError):
        await assess(sid)

    with session_factory() as session:
        actions = list(session.scalars(
            select(AuditEvent.action).where(AuditEvent.entity_type == "agent_run")))  # fmt: skip
    assert actions == ["demurrage_risk.stopped"]


# --------------------------------------------------------------------------- MCP, audit


async def test_the_agent_uses_its_three_servers_and_audits_every_figure(
    seeded: None, session_factory: sessionmaker[Session]
) -> None:
    with structlog.testing.capture_logs() as logs:
        state = await assess(shipment_id(session_factory, "HLXU7004004"))

    calls = [e for e in logs if e["event"] == "agent.tool_call"]
    assert [(e["server"], e["tool"]) for e in calls] == [
        ("shipping_records_mcp", "get_active_shipment"),
        ("demurrage_rates_mcp", "get_demurrage_charge"),
        ("fx_rates_mcp", "resolve_rate"),
    ]
    assert {e["actor"] for e in calls} == {agent.AGENT}
    with session_factory() as session:
        [event] = session.scalars(
            select(AuditEvent).where(AuditEvent.action == "demurrage_risk.completed")
        ).all()
    assert event.actor == agent.AGENT and event.entity_id == UUID(state.run_id)
    assert event.payload["risk"] == "high"
    assert event.payload["estimate"]["amount_inr"] == "141440.00"
    assert event.payload["estimate"]["chargeable_days"] == HAND_CHARGEABLE
