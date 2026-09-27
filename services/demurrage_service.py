"""Port demurrage tariffs: dated rate periods per (port, container type) and charge maths.

The single code path to demurrage data for the Demurrage dashboard, ``demurrage_rates_mcp``
and (Phase 8) the demurrage risk agent. Lookups are historical — ``get_rate`` returns the tariff
in force *on* ``as_of`` — and never fall back to a nearby or latest row: no match raises
``PortRateNotFoundError`` (problem statement §3, "raise, don't guess").

Tariff history is kept, not overwritten: a revision starting on a new day opens a new period and
closes the previous one by setting its ``effective_to``. Only a correction to a period starting
on the *same* day edits that row. A GiST exclusion constraint guarantees periods never overlap.
"""

from datetime import date, timedelta
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from models.db_models import PortRate
from models.schemas import DemurrageCharge, PortRateRecord, PortRateUpsert
from services import audit_service
from services.errors import NegativeStayError, OverlappingRateError, PortRateNotFoundError

ONE_DAY = timedelta(days=1)


def get_rate(session: Session, port_code: str, container_type: str, as_of: date) -> PortRateRecord:
    """The tariff in force at ``port_code`` for ``container_type`` on ``as_of``.

    Historical by design: a future-dated revision never changes the answer for an earlier
    date, so a shipment is always charged under the rate active when it was in port.
    """
    row = session.scalars(
        select(PortRate).where(
            PortRate.port_code == port_code,
            PortRate.container_type == container_type,
            PortRate.effective_from <= as_of,
            or_(PortRate.effective_to.is_(None), PortRate.effective_to >= as_of),
        )
    ).one_or_none()  # the exclusion constraint makes >1 impossible
    if row is None:
        raise PortRateNotFoundError(
            f"no demurrage tariff for {port_code} / {container_type} on {as_of.isoformat()}"
        )
    return PortRateRecord.model_validate(row)


def list_rates(session: Session, port_code: str | None = None) -> list[PortRateRecord]:
    """Every tariff period (optionally for one port), grouped by port and type, oldest first."""
    query = select(PortRate).order_by(
        PortRate.port_code, PortRate.container_type, PortRate.effective_from
    )
    if port_code is not None:
        query = query.where(PortRate.port_code == port_code)
    return [PortRateRecord.model_validate(row) for row in session.scalars(query)]


def list_ports(session: Session) -> list[str]:
    """Ports that have at least one tariff configured."""
    return list(session.scalars(select(PortRate.port_code).distinct().order_by(PortRate.port_code)))


def upsert_rate(session: Session, data: PortRateUpsert, actor: str) -> PortRateRecord:
    """Add a tariff period, or correct the one that starts on the same day.

    * Same ``effective_from`` as an existing period → that row's values are corrected
      (audited as ``port_rate.updated``; a no-op change writes nothing).
    * New ``effective_from`` → a new period (``port_rate.created``). The period that was in
      force on that day is closed the day before (``port_rate.superseded``).
    * ``effective_to`` left empty → runs until the day before the next scheduled period, or
      open-ended. An explicit ``effective_to`` reaching into the next period raises
      ``OverlappingRateError``.
    """
    periods = list(
        session.scalars(
            select(PortRate)
            .where(
                PortRate.port_code == data.port_code,
                PortRate.container_type == data.container_type,
            )
            .order_by(PortRate.effective_from)
            .with_for_update()  # serialise concurrent edits of the same tariff line
        )
    )
    start = data.effective_from
    same_day = next((p for p in periods if p.effective_from == start), None)
    following = next((p for p in periods if p.effective_from > start), None)
    preceding = next((p for p in reversed(periods) if p.effective_from < start), None)

    end = data.effective_to
    if end is None:
        end = following.effective_from - ONE_DAY if following else None
    elif following is not None and end >= following.effective_from:
        raise OverlappingRateError(
            f"period {start}..{end} overlaps the tariff starting {following.effective_from}"
        )

    if preceding is not None and (
        preceding.effective_to is None or preceding.effective_to >= start
    ):
        old_end = preceding.effective_to
        preceding.effective_to = start - ONE_DAY
        session.flush()  # close the old period before the new one exists (exclusion constraint)
        audit_service.record(
            session,
            actor=actor,
            action="port_rate.superseded",
            entity_type="port_rate",
            entity_id=preceding.id,
            payload={"effective_to": {"from": old_end, "to": preceding.effective_to}},
        )

    values: dict[str, Any] = {
        "currency": data.currency,
        "free_days": data.free_days,
        "daily_rate": data.daily_rate,
        "effective_to": end,
    }
    if same_day is not None:
        diff = {
            field: {"from": getattr(same_day, field), "to": new}
            for field, new in values.items()
            if getattr(same_day, field) != new
        }
        if diff:
            for field, change in diff.items():
                setattr(same_day, field, change["to"])
            session.flush()
            audit_service.record(
                session,
                actor=actor,
                action="port_rate.updated",
                entity_type="port_rate",
                entity_id=same_day.id,
                payload={"changes": diff},
            )
        session.flush()
        return PortRateRecord.model_validate(same_day)

    row = PortRate(
        port_code=data.port_code,
        container_type=data.container_type,
        effective_from=start,
        **values,
    )
    session.add(row)
    session.flush()
    record = PortRateRecord.model_validate(row)
    audit_service.record(
        session,
        actor=actor,
        action="port_rate.created",
        entity_type="port_rate",
        entity_id=row.id,
        payload={"port_rate": record.model_dump()},
    )
    session.flush()
    return record


def demurrage_charge(rate: PortRateRecord, days_in_port: int) -> DemurrageCharge:
    """Charge for ``days_in_port`` days under ``rate``, in the tariff's own currency.

    Days up to ``free_days`` are free; every day beyond is charged at ``daily_rate``. With
    ``free_days=0`` charging starts on day 1. Currency conversion is the caller's job (the
    risk agent converts at the gazetted port-charges FX rate, Phase 8).
    """
    if days_in_port < 0:
        raise NegativeStayError(f"days_in_port cannot be negative (got {days_in_port})")
    chargeable = max(0, days_in_port - rate.free_days)
    return DemurrageCharge(
        rate=rate,
        days_in_port=days_in_port,
        chargeable_days=chargeable,
        amount=rate.daily_rate * chargeable,
        currency=rate.currency,
    )
