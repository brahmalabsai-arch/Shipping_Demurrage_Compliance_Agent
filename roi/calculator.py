"""The ROI calculator (Phase 12, FR9): demurrage exposure avoided by catching document issues
before the box docks — computed only from frozen ``RoiSnapshotContent`` (conventions §9).

For every shipment present in both snapshots:

* **issues** — what the document verification agent's deterministic cross-check
  (``document_verification_agent.cross_check``, no I/O) finds on the snapshot's frozen
  documents: missing documents, quantity/value/currency disagreements, invalid currencies,
  date mismatches;
* **before** (agents disabled; ``before`` snapshot): every issue is found only by customs at the
  port, so the box stays ``base_clearance_days`` + the hold for its issues (the longest hold —
  issues worked in parallel — or their sum, per ``assumptions.combine``);
* **after** (agents enabled; ``after`` snapshot): every issue was caught and fixed before the box
  docked, so it stays ``base_clearance_days``;
* each stay is priced like Phase 8: the tariff in force on the arrival day
  (``demurrage_charge``: chargeable days = stay - free days, x daily rate, in the tariff
  currency), converted at the gazetted port-charges rate on the analysis date, rounded half-up
  to 0.01 once, after conversion;
* **avoided** = before - after (INR). With one snapshot on both sides this is exactly
  *chargeable days avoided x daily rate x FX rate*, rounded.

A shipment with no issues has the same stay in both scenarios and so contributes exactly zero.
A shipment that cannot be priced (a gap frozen in either snapshot) is excluded and listed —
never priced with a guess.
"""

from collections.abc import Callable
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from agents.demurrage_risk_agent import money
from agents.document_verification_agent import cross_check
from models.schemas import (
    RoiAssumptions,
    RoiSnapshotContent,
    RoiSnapshotRecord,
    SnapshotShipment,
)
from services.demurrage_service import demurrage_charge
from services.errors import InvalidAssumptionsError
from services.roi_service import check_assumptions

ZERO = Decimal("0.00")


class ScenarioCost(BaseModel):
    """One shipment's priced stay in one scenario, with every figure it is built from."""

    model_config = ConfigDict(frozen=True)

    scenario: Literal["before", "after"]
    base_days: int
    hold_days: int  # 0 in the after scenario
    stay_days: int  # base + hold
    free_days: int
    chargeable_days: int
    daily_rate: Decimal
    currency: str
    tariff_id: UUID
    amount: Decimal  # chargeable_days x daily_rate, tariff currency (unrounded)
    fx_rate: Decimal | None  # None when the tariff is in INR
    fx_source_id: UUID | None
    fx_basis: str | None
    amount_inr: Decimal  # rounded half-up to 0.01
    basis: str


class RoiLine(BaseModel):
    """One shipment's before/after comparison."""

    model_config = ConfigDict(frozen=True)

    shipment_id: UUID
    container_id: str
    issues: list[str]  # issue codes found without the agents (before snapshot)
    issue_details: list[str]  # the cross-check's messages
    caught: list[str]  # issue codes the agents catch pre-dock (after snapshot)
    before: ScenarioCost | None
    after: ScenarioCost | None
    chargeable_days_avoided: int
    avoided_inr: Decimal
    excluded: str | None = None  # why this shipment was not priced


class SnapshotRef(BaseModel):
    """Which snapshot a comparison read, so it can be re-run on exactly the same inputs."""

    model_config = ConfigDict(frozen=True)

    id: UUID | None
    name: str | None
    content_sha256: str | None
    as_of: str


class RoiComparison(BaseModel):
    """The ROI result: the totals and every line they are summed from."""

    model_config = ConfigDict(frozen=True)

    before_snapshot: SnapshotRef
    after_snapshot: SnapshotRef
    assumptions_before: RoiAssumptions
    assumptions_after: RoiAssumptions
    lines: list[RoiLine]
    total_before_inr: Decimal
    total_after_inr: Decimal
    total_avoided_inr: Decimal
    excluded: list[str]


def issue_codes(item: SnapshotShipment) -> tuple[list[str], list[str]]:
    """The cross-check's issue codes (distinct, in order) and messages for a frozen shipment."""
    findings = cross_check(item.shipment, list(item.documents)).findings
    return list(dict.fromkeys(f.code for f in findings)), [f.message for f in findings]


def hold_for(codes: list[str], assumptions: RoiAssumptions) -> int:
    """Days customs holds a box for these issues, under the assumptions."""
    holds = []
    for code in codes:
        if code not in assumptions.hold_days:
            raise InvalidAssumptionsError(f"no hold duration for issue type {code!r}")
        holds.append(assumptions.hold_days[code])
    if not holds:
        return 0
    return max(holds) if assumptions.combine == "max" else sum(holds)


def price(
    item: SnapshotShipment, scenario: Literal["before", "after"], base_days: int, hold_days: int
) -> ScenarioCost:
    """Price ``base_days + hold_days`` in port with the snapshot's tariff and FX rate."""
    assert item.tariff is not None  # callers exclude gaps first
    stay = base_days + hold_days
    charge = demurrage_charge(item.tariff, stay)
    tariff = item.tariff
    if tariff.currency == "INR":
        amount_inr, fx_text = money(charge.amount), ""
    else:
        assert item.fx is not None
        amount_inr = money(charge.amount * item.fx.rate)
        fx_text = f" x {item.fx.rate} {item.fx.pair}"
    held = f" + {hold_days} held" if hold_days else ""
    basis = (
        f"{base_days}{held} = {stay} days in port, {tariff.free_days} free -> "
        f"{charge.chargeable_days} chargeable x {tariff.currency} {tariff.daily_rate} = "
        f"{tariff.currency} {charge.amount}{fx_text} = INR {amount_inr}"
    )
    return ScenarioCost(
        scenario=scenario,
        base_days=base_days,
        hold_days=hold_days,
        stay_days=stay,
        free_days=tariff.free_days,
        chargeable_days=charge.chargeable_days,
        daily_rate=tariff.daily_rate,
        currency=tariff.currency,
        tariff_id=tariff.id,
        amount=charge.amount,
        fx_rate=None if item.fx is None else item.fx.rate,
        fx_source_id=None if item.fx is None else item.fx.source_id,
        fx_basis=None if item.fx is None else item.fx.basis,
        amount_inr=amount_inr,
        basis=basis,
    )


def _gap(item: SnapshotShipment) -> str | None:
    if item.gaps:
        return "; ".join(item.gaps)
    if item.tariff is None:
        return "no tariff frozen"
    if item.tariff.currency != "INR" and item.fx is None:
        return "no FX rate frozen"
    return None


def _ref(snapshot: RoiSnapshotRecord | RoiSnapshotContent) -> SnapshotRef:
    if isinstance(snapshot, RoiSnapshotRecord):
        return SnapshotRef(id=snapshot.id, name=snapshot.name,
                           content_sha256=snapshot.content_sha256,
                           as_of=snapshot.as_of.isoformat())  # fmt: skip
    return SnapshotRef(id=None, name=None, content_sha256=None, as_of=snapshot.as_of.isoformat())


def _content(snapshot: RoiSnapshotRecord | RoiSnapshotContent) -> RoiSnapshotContent:
    return snapshot.content if isinstance(snapshot, RoiSnapshotRecord) else snapshot


def compare(
    before: RoiSnapshotRecord | RoiSnapshotContent,
    after: RoiSnapshotRecord | RoiSnapshotContent,
    *,
    on_line: Callable[[RoiLine], None] | None = None,
) -> RoiComparison:
    """Before/after demurrage exposure of every shipment in both snapshots, and the total
    avoided. Reads nothing but the two snapshots (``on_line`` is called after each line — e.g.
    for progress — and cannot change the result)."""
    b, a = _content(before), _content(after)
    check_assumptions(b.assumptions)
    check_assumptions(a.assumptions)
    after_by_id = {item.shipment.id: item for item in a.shipments}
    lines: list[RoiLine] = []
    excluded: list[str] = []

    for item in b.shipments:
        cid = item.shipment.container_id
        twin = after_by_id.get(item.shipment.id)
        codes, details = issue_codes(item)
        caught = issue_codes(twin)[0] if twin is not None else []
        reason = (
            "not in the after snapshot" if twin is None else _gap(item) or _gap(twin)
        )  # fmt: skip
        if reason is not None or twin is None:
            line = RoiLine(shipment_id=item.shipment.id, container_id=cid, issues=codes,
                           issue_details=details, caught=caught, before=None, after=None,
                           chargeable_days_avoided=0, avoided_inr=ZERO, excluded=reason)  # fmt: skip
            excluded.append(f"{cid}: {reason}")
        else:
            cost_before = price(item, "before", b.assumptions.base_clearance_days,
                                hold_for(codes, b.assumptions))  # fmt: skip
            cost_after = price(twin, "after", a.assumptions.base_clearance_days, 0)
            line = RoiLine(
                shipment_id=item.shipment.id,
                container_id=cid,
                issues=codes,
                issue_details=details,
                caught=caught,
                before=cost_before,
                after=cost_after,
                chargeable_days_avoided=cost_before.chargeable_days - cost_after.chargeable_days,
                avoided_inr=cost_before.amount_inr - cost_after.amount_inr,
            )
        lines.append(line)
        if on_line is not None:
            on_line(line)

    before_ids = {item.shipment.id for item in b.shipments}
    excluded += [f"{item.shipment.container_id}: not in the before snapshot"
                 for item in a.shipments if item.shipment.id not in before_ids]  # fmt: skip
    priced = [line for line in lines if line.excluded is None]
    total_before = sum((line.before.amount_inr for line in priced if line.before), ZERO)
    total_after = sum((line.after.amount_inr for line in priced if line.after), ZERO)
    return RoiComparison(
        before_snapshot=_ref(before),
        after_snapshot=_ref(after),
        assumptions_before=b.assumptions,
        assumptions_after=a.assumptions,
        lines=lines,
        total_before_inr=total_before,
        total_after_inr=total_after,
        total_avoided_inr=total_before - total_after,
        excluded=excluded,
    )
