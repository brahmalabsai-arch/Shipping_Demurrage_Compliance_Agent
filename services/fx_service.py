"""Exchange rates: spot, locked and gazetted tables, and the four-stage resolution engine.

The single code path to FX data for the FX Rates dashboard, ``fx_rates_mcp`` and the agents
(architecture §4). ``resolve_rate`` applies the rule for the shipment's lifecycle stage
(architecture §8) and never substitutes a nearby date or a different stage's rate: when the rule
finds nothing it raises ``RateNotFoundError`` (problem statement §3, "raise, don't guess").

* quotation    — latest spot observation on or before the quote day; valid for 7-14 days.
* bol          — the rate locked on the bill-of-lading date, exactly.
* invoice      — commercial invoice: the rate locked on its own invoice date; freight invoice:
                 the rate locked on the BoL date (the caller passes ``bol_date``).
* port_charges — the gazetted rate whose period contains the date, preferring a revision.

Locked and gazetted rates are append-only (a database trigger rejects UPDATE/DELETE); a wrong
gazetted rate is corrected by publishing a revision of it. Spot observations may be corrected in
place, like demurrage tariffs.
"""

from datetime import date, datetime, time, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select, union
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from models.db_models import GazettedRate, LockedRate, SpotRate
from models.enums import FxStage, InvoiceKind, LockType, RateSource
from models.schemas import (
    GazettedRateCreate,
    GazettedRateRecord,
    LockedRateCreate,
    LockedRateRecord,
    ResolvedRate,
    SpotRateRecord,
    SpotRateUpsert,
)
from services import audit_service
from services.errors import (
    GazettedRateNotFoundError,
    InvalidGazettedRevisionError,
    InvalidRateRequestError,
    OverlappingGazettedPeriodError,
    RateAlreadyLockedError,
    RateNotFoundError,
)

# The FX rules key off the calendar day (conventions §3); for this Indian importer that is the
# day in IST. A spot observation at 23:00 IST on the quote day counts; 00:30 IST the next day
# does not.
BUSINESS_TZ = ZoneInfo("Asia/Kolkata")
QUOTE_VALIDITY_DAYS = range(7, 15)  # 7-14 inclusive (problem statement, quotation stage)


# --------------------------------------------------------------------------- spot rates


def get_spot_rate(session: Session, pair: str, as_of: date) -> SpotRateRecord:
    """The latest spot observation for ``pair`` made on or before the IST calendar day ``as_of``.

    Never looks forward: an observation later than that day is ignored even if it is the only one.
    """
    cutoff = datetime.combine(as_of + timedelta(days=1), time(0), tzinfo=BUSINESS_TZ)
    row = session.scalars(
        select(SpotRate)
        .where(SpotRate.pair == pair, SpotRate.as_of < cutoff)
        .order_by(SpotRate.as_of.desc())
        .limit(1)
    ).one_or_none()
    if row is None:
        raise RateNotFoundError(f"no {pair} spot rate observed on or before {as_of.isoformat()}")
    return SpotRateRecord.model_validate(row)


def list_spot_rates(session: Session, pair: str | None = None) -> list[SpotRateRecord]:
    """Spot observations (optionally for one pair), newest first."""
    query = select(SpotRate).order_by(SpotRate.pair, SpotRate.as_of.desc())
    if pair is not None:
        query = query.where(SpotRate.pair == pair)
    return [SpotRateRecord.model_validate(row) for row in session.scalars(query)]


def upsert_spot_rate(session: Session, data: SpotRateUpsert, actor: str) -> SpotRateRecord:
    """Record a spot observation, or correct the one already recorded for the same instant.

    A correction is audited as ``spot_rate.updated`` with a from/to diff; an identical re-entry
    writes nothing.
    """
    existing = session.scalars(
        select(SpotRate)
        .where(SpotRate.pair == data.pair, SpotRate.as_of == data.as_of)
        .with_for_update()
    ).one_or_none()

    if existing is not None:
        values = {"rate": data.rate, "source": data.source}
        diff = {
            field: {"from": getattr(existing, field), "to": new}
            for field, new in values.items()
            if getattr(existing, field) != new
        }
        if diff:
            for field, change in diff.items():
                setattr(existing, field, change["to"])
            session.flush()
            audit_service.record(
                session,
                actor=actor,
                action="spot_rate.updated",
                entity_type="spot_rate",
                entity_id=existing.id,
                payload={"changes": diff},
            )
            session.flush()
        return SpotRateRecord.model_validate(existing)

    row = SpotRate(**data.model_dump())
    session.add(row)
    session.flush()
    record = SpotRateRecord.model_validate(row)
    audit_service.record(
        session,
        actor=actor,
        action="spot_rate.created",
        entity_type="spot_rate",
        entity_id=row.id,
        payload={"spot_rate": record.model_dump()},
    )
    session.flush()
    return record


# --------------------------------------------------------------------------- locked rates


def get_locked_rate(
    session: Session, pair: str, lock_type: LockType, lock_date: date
) -> LockedRateRecord:
    """The rate locked for ``pair`` on exactly ``lock_date`` — no nearest-date substitute."""
    row = session.scalars(
        select(LockedRate).where(
            LockedRate.pair == pair,
            LockedRate.lock_type == lock_type,
            LockedRate.lock_date == lock_date,
        )
    ).one_or_none()  # unique constraint makes >1 impossible
    if row is None:
        raise RateNotFoundError(
            f"no {pair} rate locked for {lock_type.value} on {lock_date.isoformat()}"
        )
    return LockedRateRecord.model_validate(row)


def list_locked_rates(session: Session, pair: str | None = None) -> list[LockedRateRecord]:
    """Locked rates (optionally for one pair), newest lock date first."""
    query = select(LockedRate).order_by(
        LockedRate.pair, LockedRate.lock_date.desc(), LockedRate.lock_type
    )
    if pair is not None:
        query = query.where(LockedRate.pair == pair)
    return [LockedRateRecord.model_validate(row) for row in session.scalars(query)]


def lock_rate(session: Session, data: LockedRateCreate, actor: str) -> LockedRateRecord:
    """Lock a rate for a BoL or commercial-invoice date. Once locked it can never change.

    A second lock for the same pair, type and date raises ``RateAlreadyLockedError`` rather than
    overwriting — documents already priced at the locked rate must stay reproducible.
    """
    row = LockedRate(**data.model_dump())
    try:
        with session.begin_nested():  # a failed insert must not poison the caller's transaction
            session.add(row)
            session.flush()
    except IntegrityError as exc:
        if "uq_locked_rates_pair_lock_type_lock_date" in str(exc.orig):
            existing = get_locked_rate(session, data.pair, data.lock_type, data.lock_date)
            raise RateAlreadyLockedError(
                f"{data.pair} is already locked at {existing.rate} for "
                f"{data.lock_type.value} on {data.lock_date.isoformat()}; locked rates are "
                "immutable"
            ) from exc
        raise

    record = LockedRateRecord.model_validate(row)
    audit_service.record(
        session,
        actor=actor,
        action="locked_rate.created",
        entity_type="locked_rate",
        entity_id=row.id,
        payload={"locked_rate": record.model_dump()},
    )
    session.flush()
    return record


# --------------------------------------------------------------------------- gazetted rates


def get_gazetted_rate(session: Session, pair: str, as_of: date) -> GazettedRateRecord:
    """The gazetted rate in force for ``pair`` on ``as_of``, corrections applied.

    Finds the original notification whose period contains the date, then descends through its
    revisions: at each level at most one revision covers the date (exclusion constraint), and
    the deepest one wins — so a correction of a correction is honoured.
    """
    rows = list(
        session.scalars(
            select(GazettedRate).where(
                GazettedRate.pair == pair,
                GazettedRate.period_start <= as_of,
                GazettedRate.period_end >= as_of,
            )
        )
    )
    children: dict[UUID | None, GazettedRate] = {row.revision_of: row for row in rows}
    current = children.get(None)
    if current is None:
        raise RateNotFoundError(f"no gazetted {pair} rate covers {as_of.isoformat()}")
    while current.id in children:
        current = children[current.id]
    return GazettedRateRecord.model_validate(current)


def list_gazetted_rates(session: Session, pair: str | None = None) -> list[GazettedRateRecord]:
    """Every gazetted row (originals and revisions), by pair and period."""
    query = select(GazettedRate).order_by(
        GazettedRate.pair,
        GazettedRate.period_start,
        GazettedRate.revision_of.is_not(None),  # original before its revisions
        GazettedRate.period_end,
    )
    if pair is not None:
        query = query.where(GazettedRate.pair == pair)
    return [GazettedRateRecord.model_validate(row) for row in session.scalars(query)]


def publish_gazetted_rate(
    session: Session, data: GazettedRateCreate, actor: str
) -> GazettedRateRecord:
    """Publish a gazetted notification, or a revision correcting an existing row.

    Originals for a pair may not overlap (``OverlappingGazettedPeriodError``). A revision must be
    for the same pair and fall within the revised row's period (``InvalidGazettedRevisionError``);
    two revisions of the same row may not overlap. Audited as ``gazetted_rate.created`` or
    ``gazetted_rate.revised``.
    """
    parent: GazettedRate | None = None
    if data.revision_of is not None:
        parent = session.get(GazettedRate, data.revision_of)
        if parent is None:
            raise GazettedRateNotFoundError(f"no gazetted rate with id {data.revision_of}")
        if parent.pair != data.pair:
            raise InvalidGazettedRevisionError(
                f"a revision of a {parent.pair} rate cannot be for {data.pair}"
            )
        if data.period_start < parent.period_start or data.period_end > parent.period_end:
            raise InvalidGazettedRevisionError(
                f"revision period {data.period_start}..{data.period_end} must lie within the "
                f"revised period {parent.period_start}..{parent.period_end}"
            )

    row = GazettedRate(**data.model_dump())
    try:
        with session.begin_nested():
            session.add(row)
            session.flush()
    except IntegrityError as exc:
        if "ex_gazetted_rates_no_overlap" in str(exc.orig):
            raise OverlappingGazettedPeriodError(
                f"{data.pair} {data.period_start}..{data.period_end} overlaps an existing "
                "notification; publish a revision of it instead"
            ) from exc
        if "ex_gazetted_rates_revision_no_overlap" in str(exc.orig):
            raise OverlappingGazettedPeriodError(
                f"{data.period_start}..{data.period_end} overlaps another revision of the same "
                "row; revise that revision instead"
            ) from exc
        raise

    record = GazettedRateRecord.model_validate(row)
    audit_service.record(
        session,
        actor=actor,
        action="gazetted_rate.revised" if parent is not None else "gazetted_rate.created",
        entity_type="gazetted_rate",
        entity_id=row.id,
        payload={"gazetted_rate": record.model_dump()},
    )
    session.flush()
    return record


def list_pairs(session: Session) -> list[str]:
    """Currency pairs present in any of the three rate tables."""
    pairs = union(
        select(SpotRate.pair), select(LockedRate.pair), select(GazettedRate.pair)
    ).subquery()
    return list(session.scalars(select(pairs.c.pair).order_by(pairs.c.pair)))


# --------------------------------------------------------------------------- resolution engine


def resolve_rate(
    session: Session,
    pair: str,
    as_of: date,
    stage: FxStage,
    *,
    quote_validity_days: int | None = None,
    invoice_kind: InvoiceKind | None = None,
    bol_date: date | None = None,
) -> ResolvedRate:
    """The FX rate for ``pair`` at lifecycle ``stage`` on ``as_of`` (architecture §8).

    Stage context is strict so a caller can never get a different rule than it thinks it asked
    for: ``quote_validity_days`` (7-14) is required for quotations; ``invoice_kind`` for
    invoices; ``bol_date`` for freight invoices only. Missing or irrelevant context raises
    ``InvalidRateRequestError``; a rule that finds no rate raises ``RateNotFoundError``.
    """
    _check_context(stage, quote_validity_days, invoice_kind, bol_date)
    common: dict[str, Any] = {"pair": pair, "stage": stage, "as_of": as_of}

    if stage is FxStage.QUOTATION:
        assert quote_validity_days is not None  # guaranteed by _check_context
        spot = get_spot_rate(session, pair, as_of)
        observed = spot.as_of.astimezone(BUSINESS_TZ).strftime("%Y-%m-%d %H:%M IST")
        return ResolvedRate(
            **common,
            rate=spot.rate,
            source=RateSource.SPOT,
            source_id=spot.id,
            basis=f"spot observed {observed} ({spot.source})",
            locked=False,
            expires=as_of + timedelta(days=quote_validity_days),
        )

    if stage is FxStage.PORT_CHARGES:
        gazetted = get_gazetted_rate(session, pair, as_of)
        revision = f", revision of {gazetted.revision_of}" if gazetted.revision_of else ""
        return ResolvedRate(
            **common,
            rate=gazetted.rate,
            source=RateSource.GAZETTED,
            source_id=gazetted.id,
            basis=(
                f"gazetted {gazetted.period_start}..{gazetted.period_end} by "
                f"{gazetted.publishing_authority}{revision}"
            ),
            locked=False,
        )

    # bol / invoice: an exact locked-rate lookup.
    if stage is FxStage.BOL:
        lock_type, lock_date = LockType.BOL, as_of
    elif invoice_kind is InvoiceKind.FREIGHT:
        assert bol_date is not None  # guaranteed by _check_context
        lock_type, lock_date = LockType.BOL, bol_date
    else:
        lock_type, lock_date = LockType.COMMERCIAL_INVOICE, as_of
    locked = get_locked_rate(session, pair, lock_type, lock_date)
    return ResolvedRate(
        **common,
        rate=locked.rate,
        source=RateSource.LOCKED,
        source_id=locked.id,
        basis=f"locked on {lock_type.value} date {lock_date.isoformat()}",
        locked=True,
        invoice_kind=invoice_kind,
    )


def _check_context(
    stage: FxStage,
    quote_validity_days: int | None,
    invoice_kind: InvoiceKind | None,
    bol_date: date | None,
) -> None:
    given = {
        "quote_validity_days": quote_validity_days is not None,
        "invoice_kind": invoice_kind is not None,
        "bol_date": bol_date is not None,
    }
    needed = {
        FxStage.QUOTATION: {"quote_validity_days"},
        FxStage.BOL: set(),
        FxStage.INVOICE: (
            {"invoice_kind", "bol_date"}
            if invoice_kind is InvoiceKind.FREIGHT
            else {"invoice_kind"}
        ),
        FxStage.PORT_CHARGES: set(),
    }[stage]
    missing = sorted(name for name in needed if not given[name])
    extra = sorted(name for name, present in given.items() if present and name not in needed)
    if missing or extra:
        problems = [f"missing {', '.join(missing)}"] if missing else []
        problems += [f"not used by this stage: {', '.join(extra)}"] if extra else []
        raise InvalidRateRequestError(f"stage '{stage.value}': {'; '.join(problems)}")
    if quote_validity_days is not None and quote_validity_days not in QUOTE_VALIDITY_DAYS:
        raise InvalidRateRequestError(
            f"quote_validity_days must be 7-14 (got {quote_validity_days})"
        )


def is_quote_expired(resolved: ResolvedRate, on: date) -> bool:
    """Whether a quotation-stage rate is no longer valid on ``on`` (valid through ``expires``).

    Callers — the dashboard and the document verification agent — use this to surface
    "quote expired" instead of silently pricing with a stale quotation.
    """
    if resolved.stage is not FxStage.QUOTATION or resolved.expires is None:
        raise InvalidRateRequestError("only a quotation-stage rate has a validity window")
    return on > resolved.expires
