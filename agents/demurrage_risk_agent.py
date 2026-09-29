"""Demurrage & Risk agent — what a shipment's time in port costs, in the tariff's currency and in
rupees, and how likely a costly hold is (problem statement FR4; implementation plan Phase 8;
edge cases, Phases 1, 2 and 8).

Through MCP only (``shipping_records_mcp``, ``demurrage_rates_mcp``, ``fx_rates_mcp``), for one
shipment on the analysis date ``as_of``:

1. ``get_active_shipment`` — a cancelled shipment stops the run with ``ShipmentCancelledError``.
   A cleared/closed shipment has left the port: risk ``none``, no estimate.
2. The arrival day is the IST day of the ETA (the shipments table has no separate dock date).
   No ETA → risk ``indeterminate`` with a data gap — never a silent zero.
3. ``get_demurrage_charge(destination_port, container_type, as_of=<arrival day>,
   days_in_port)`` — the tariff in force on the arrival day prices the whole stay, so a tariff
   revised after docking does not reprice it. Days in port count the arrival day as day 1,
   through ``as_of`` (0 before arrival); ``free_days=0`` charges from day 1. No tariff →
   ``indeterminate`` with a data gap.
4. ``resolve_rate(<tariff currency>/INR, as_of, "port_charges")`` — the gazetted rate in force
   on the analysis date. No rate → a data gap and a *partial* estimate in the tariff currency
   (never raised, never silently dropped).
5. Risk, deterministically: ``high`` while demurrage accrues or customs holds the container;
   ``medium`` when a hold looks likely (document discrepancies from the verification agent,
   free time ending within a day, an ETA passed without docking); ``low`` otherwise. Each level
   comes with its reasons.

Money is rounded once, half-up to 0.01, after conversion; every intermediate figure is kept in
:class:`~agents.state.DemurrageEstimate` so the number can be rebuilt by hand (conventions §9).
No LLM: the ``basis`` sentence is built from the figures. The agent changes no domain data;
its outcome is an ``agent_run`` audit event (``demurrage_risk.completed``, or ``.stopped``).

State slice: reads ``CaseState.run_id``, ``shipment_id``, ``as_of`` and
``document_discrepancies`` (empty unless document verification ran first); writes
``demurrage_estimate``, ``risk``, ``risk_reasons`` and ``data_gaps``.

Run by hand: ``python -m scripts.analyse_shipments --all`` (with document verification first).
"""

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from uuid import UUID, uuid4

import structlog
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from agents.audit import record_run_event
from agents.ist import ist_day, today_ist
from agents.mcp_clients import Connector, McpToolbox, open_toolbox, stdio_connector
from agents.state import CaseState, DemurrageEstimate, Discrepancy, RiskLevel
from models.enums import FxStage, ShipmentStatus
from models.schemas import DemurrageCharge, ResolvedRate, ShipmentRecord
from services.errors import McpToolError, ShipmentCancelledError

AGENT = "demurrage_risk_agent"
SHIPPING, DEMURRAGE, FX = "shipping_records_mcp", "demurrage_rates_mcp", "fx_rates_mcp"
SERVERS = (SHIPPING, DEMURRAGE, FX)

HOME_CURRENCY = "INR"
CENT = Decimal("0.01")
IN_PORT = frozenset({ShipmentStatus.DOCKED, ShipmentStatus.UNDER_REVIEW, ShipmentStatus.HELD})
RELEASED = frozenset({ShipmentStatus.CLEARED, ShipmentStatus.CLOSED})

logger = structlog.get_logger("agents")


@dataclass(frozen=True)
class RiskDeps:
    """What the node needs besides state (passed as ``config["configurable"][AGENT]``)."""

    toolbox: McpToolbox


@dataclass
class Assessment:
    """The agent's result before it is written into ``CaseState``."""

    estimate: DemurrageEstimate | None
    risk: RiskLevel
    reasons: list[str]
    gaps: list[str] = field(default_factory=list)


def money(value: Decimal) -> Decimal:
    """Round a money amount half-up to 0.01 (done once, at the end of a computation)."""
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def days_in_port(arrival: date, as_of: date, arrived: bool) -> int:
    """Days at the port on ``as_of``, the arrival day counted as day 1; 0 before arrival."""
    if not arrived or as_of < arrival:
        return 0
    return (as_of - arrival).days + 1


# --------------------------------------------------------------------------- estimate


async def _convert(
    tools: McpToolbox, charge: DemurrageCharge, as_of: date, gaps: list[str]
) -> tuple[ResolvedRate | None, Decimal | None, Decimal | None]:
    """(gazetted rate, amount in INR, daily rate in INR); rate None when not needed or missing."""
    if charge.currency == HOME_CURRENCY:
        return None, money(charge.amount), money(charge.rate.daily_rate)
    pair = f"{charge.currency}/{HOME_CURRENCY}"
    try:
        raw = await tools.call(FX, "resolve_rate",
                               {"pair": pair, "as_of": as_of, "stage": FxStage.PORT_CHARGES})  # fmt: skip
    except McpToolError as exc:
        if exc.error_type != "RateNotFoundError":
            raise
        gaps.append(
            f"fx_rate: no gazetted {pair} rate for {as_of} ({exc.message}); the estimate stays "
            f"in {charge.currency}"
        )
        return None, None, None
    fx = ResolvedRate.model_validate(raw)
    return fx, money(charge.amount * fx.rate), money(charge.rate.daily_rate * fx.rate)


def _basis(e: DemurrageEstimate) -> str:
    tariff = (
        f"{e.port_code} {e.container_type.value} tariff in force on {e.arrival_date}: "
        f"{e.free_days} free day(s), then {e.daily_rate} {e.currency}/day."
    )
    if not e.arrived:
        return f"{tariff} Not in port yet (ETA {e.arrival_date}); charges would start on {e.charges_from}."
    text = (
        f"{tariff} {e.days_in_port} day(s) in port ({e.arrival_date} to {e.as_of}, arrival day "
        f"counted) - {e.free_days} free = {e.chargeable_days} chargeable x {e.daily_rate} = "
        f"{e.amount} {e.currency}"
    )
    if e.fx is not None and e.amount_inr is not None:
        text += f" x {e.fx.pair} {e.fx.rate} (gazetted, {e.fx.as_of}) = INR {e.amount_inr}"
    return text + "."


async def estimate_demurrage(
    tools: McpToolbox, shipment: ShipmentRecord, as_of: date, gaps: list[str]
) -> DemurrageEstimate | None:
    """The priced stay (None if it cannot be estimated — the reason is added to ``gaps``)."""
    if shipment.eta is None:
        gaps.append("eta: not set on the shipment record, so the arrival day is unknown")
        return None
    arrival = ist_day(shipment.eta)
    arrived = shipment.status in IN_PORT
    if arrived and as_of < arrival:
        gaps.append(
            f"eta: status is {shipment.status.value} but the ETA {arrival} is after the analysis "
            f"date {as_of}; counted as 0 days in port"
        )
    days = days_in_port(arrival, as_of, arrived)
    try:
        raw = await tools.call(DEMURRAGE, "get_demurrage_charge", {
            "port_code": shipment.destination_port, "container_type": shipment.container_type,
            "as_of": arrival, "days_in_port": days})  # fmt: skip
    except McpToolError as exc:
        if exc.error_type != "PortRateNotFoundError":
            raise
        gaps.append(f"tariff: {exc.message}")
        return None
    charge = DemurrageCharge.model_validate(raw)
    fx, amount_inr, daily_inr = await _convert(tools, charge, as_of, gaps)
    estimate = DemurrageEstimate(
        port_code=shipment.destination_port,
        container_type=shipment.container_type,
        arrival_date=arrival,
        arrived=arrived,
        as_of=as_of,
        days_in_port=days,
        free_days=charge.rate.free_days,
        charges_from=arrival + timedelta(days=charge.rate.free_days),
        chargeable_days=charge.chargeable_days,
        daily_rate=charge.rate.daily_rate,
        currency=charge.currency,
        tariff_id=charge.rate.id,
        amount=money(charge.amount),
        fx=fx,
        amount_inr=amount_inr,
        daily_rate_inr=daily_inr,
        basis="",
    )
    return estimate.model_copy(update={"basis": _basis(estimate)})


# --------------------------------------------------------------------------- risk

_RANK: dict[RiskLevel, int] = {"low": 0, "medium": 1, "high": 2}


def assess_risk(
    shipment: ShipmentRecord, estimate: DemurrageEstimate, discrepancies: list[Discrepancy]
) -> tuple[RiskLevel, list[str]]:
    """The hold/cost risk of a priced stay, with the reason for every step up."""
    level: RiskLevel = "low"
    reasons: list[str] = []

    def note(at: RiskLevel, reason: str) -> None:
        nonlocal level
        if _RANK[at] > _RANK[level]:
            level = at
        reasons.append(reason)

    e = estimate
    last_free_day = e.charges_from - timedelta(days=1)
    if e.arrived:
        if e.chargeable_days > 0:
            so_far = f"{e.amount} {e.currency}"
            if e.amount_inr is not None and e.currency != HOME_CURRENCY:
                so_far += f" (INR {e.amount_inr})"
            note("high", f"demurrage accruing since {e.charges_from}: {e.chargeable_days} "
                         f"chargeable day(s), {so_far} so far")  # fmt: skip
        elif (e.charges_from - e.as_of).days <= 1:
            note("medium", f"free time ends {last_free_day}; charges start {e.charges_from}")
        else:
            note("low", f"in free time until {last_free_day}")
        if shipment.status is ShipmentStatus.HELD:
            note("high", "held by customs: charges keep accruing until the container is released")
    elif e.as_of > e.arrival_date:
        note("medium", f"ETA {e.arrival_date} has passed but the shipment is not docked "
                       f"(status {shipment.status.value})")  # fmt: skip
    else:
        note("low", f"arrives {e.arrival_date}; free time until {last_free_day}")
    if discrepancies:
        codes = ", ".join(sorted({d.code for d in discrepancies}))
        note("medium", f"{len(discrepancies)} document discrepancy(ies) ({codes}): a customs hold "
                       "is likely until they are resolved")  # fmt: skip
    return level, reasons


async def assess(
    tools: McpToolbox, shipment: ShipmentRecord, as_of: date, discrepancies: list[Discrepancy]
) -> Assessment:
    """Estimate the stay and rate the risk; a gap never raises."""
    if shipment.status in RELEASED:
        return Assessment(None, "none", [
            f"shipment {shipment.status.value}: the container has left the port, no further "
            "port charges accrue"])  # fmt: skip
    gaps: list[str] = []
    estimate = await estimate_demurrage(tools, shipment, as_of, gaps)
    if estimate is None:
        return Assessment(None, "indeterminate", [
            "demurrage cannot be estimated without an arrival day and a tariff (see data gaps)"],
            gaps)  # fmt: skip
    risk, reasons = assess_risk(shipment, estimate, discrepancies)
    return Assessment(estimate, risk, reasons, gaps)


# --------------------------------------------------------------------------- the node


async def demurrage_risk_agent(state: CaseState, config: RunnableConfig) -> dict[str, Any]:
    """LangGraph node: read the active shipment → price its stay → convert → rate the risk."""
    deps: RiskDeps = config["configurable"][AGENT]
    tools = deps.toolbox
    try:
        raw = await tools.call(SHIPPING, "get_active_shipment", {"shipment_id": state.shipment_id})
    except McpToolError as exc:
        if exc.error_type != "ShipmentCancelledError":
            raise
        record_run_event(AGENT, state.run_id, "demurrage_risk.stopped",
                         {"shipment_id": str(state.shipment_id), "reason": exc.message})  # fmt: skip
        raise ShipmentCancelledError(exc.message) from exc
    shipment = ShipmentRecord.model_validate(raw)

    result = await assess(tools, shipment, state.as_of, state.document_discrepancies)

    estimate = result.estimate
    record_run_event(AGENT, state.run_id, "demurrage_risk.completed", {
        "shipment_id": str(shipment.id),
        "container_id": shipment.container_id,
        "as_of": state.as_of.isoformat(),
        "risk": result.risk,
        "risk_reasons": result.reasons,
        "data_gaps": result.gaps,
        "estimate": None if estimate is None else estimate.model_dump(mode="json"),
        "document_discrepancies_considered": len(state.document_discrepancies),
    })  # fmt: skip
    logger.info("agent.run_completed", agent=AGENT, run_id=state.run_id,
                container_id=shipment.container_id, risk=result.risk,
                amount_inr=None if estimate is None else str(estimate.amount_inr))  # fmt: skip
    return {
        "demurrage_estimate": estimate,
        "risk": result.risk,
        "risk_reasons": result.reasons,
        "data_gaps": result.gaps,
    }


def build_graph() -> CompiledStateGraph[CaseState, Any, CaseState, CaseState]:
    """This agent alone; :mod:`agents.graph` runs it after document verification."""
    graph = StateGraph(CaseState)
    graph.add_node("demurrage_risk_agent", demurrage_risk_agent)
    graph.add_edge(START, "demurrage_risk_agent")
    graph.add_edge("demurrage_risk_agent", END)
    return graph.compile()


async def run(deps: RiskDeps, shipment_id: UUID, *, as_of: date, run_id: str) -> CaseState:
    """Assess one shipment on its own (no document discrepancies known) and return the state."""
    result = await build_graph().ainvoke(
        CaseState(run_id=run_id, shipment_id=shipment_id, as_of=as_of),
        config={"configurable": {AGENT: deps}},
    )
    return CaseState.model_validate(result)


async def assess_shipment(
    shipment_id: UUID,
    *,
    connector: Connector | None = None,
    as_of: date | None = None,
    run_id: str | None = None,
) -> CaseState:
    """Connect to this agent's MCP servers and assess one shipment (default: today, IST)."""
    run_id = run_id or str(uuid4())
    async with open_toolbox(
        SERVERS, actor=AGENT, run_id=run_id, connector=connector or stdio_connector
    ) as toolbox:
        return await run(RiskDeps(toolbox), shipment_id, as_of=as_of or today_ist(), run_id=run_id)
