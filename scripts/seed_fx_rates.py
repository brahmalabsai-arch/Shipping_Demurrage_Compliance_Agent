"""Seed the FX spot, locked and gazetted rates for the JNPT case study (USD/EUR/JPY into INR).

Idempotent: a spot observation for the same pair and instant, a lock for the same pair, type and
date, or a gazetted row for the same pair and period start (and, for a revision, the same revised
row) is skipped untouched — re-running never duplicates rows and never overwrites dashboard edits.

    .venv\\Scripts\\python.exe -m scripts.seed_fx_rates
"""

from dataclasses import dataclass
from typing import Literal

from sqlalchemy.orm import Session

from models.schemas import LockedRateCreate, SpotRateUpsert
from services import fx_service
from services.db import session_scope
from services.errors import RateNotFoundError
from tests.fixtures.fx_rates import (
    SEED_GAZETTED_RATES,
    SEED_LOCKED_RATES,
    SEED_SPOT_RATES,
    GazettedSeed,
)

ACTOR = "seed_script"


@dataclass(frozen=True)
class SeedResult:
    label: str
    outcome: Literal["created", "skipped"]


def seed_spot(session: Session, rate: SpotRateUpsert) -> SeedResult:
    """Add one spot observation unless one exists for the same pair and instant."""
    label = f"spot {rate.pair} at {rate.as_of.isoformat()}"
    if any(r.as_of == rate.as_of for r in fx_service.list_spot_rates(session, rate.pair)):
        return SeedResult(label, "skipped")
    fx_service.upsert_spot_rate(session, rate, ACTOR)
    return SeedResult(label, "created")


def seed_locked(session: Session, rate: LockedRateCreate) -> SeedResult:
    """Lock one rate unless that pair is already locked for the same type and date."""
    label = f"locked {rate.pair} {rate.lock_type.value} on {rate.lock_date}"
    try:
        fx_service.get_locked_rate(session, rate.pair, rate.lock_type, rate.lock_date)
        return SeedResult(label, "skipped")
    except RateNotFoundError:
        pass
    fx_service.lock_rate(session, rate, ACTOR)
    return SeedResult(label, "created")


def seed_gazetted(session: Session, seed: GazettedSeed) -> SeedResult:
    """Publish one gazetted notification or revision unless it is already there."""
    rows = fx_service.list_gazetted_rates(session, seed.pair)
    originals = {r.period_start: r for r in rows if r.revision_of is None}
    if seed.revises_period_start is None:
        label = f"gazetted {seed.pair} {seed.period_start}..{seed.period_end}"
        if seed.period_start in originals:
            return SeedResult(label, "skipped")
        fx_service.publish_gazetted_rate(session, seed.to_create(), ACTOR)
        return SeedResult(label, "created")

    parent = originals[seed.revises_period_start]  # originals are seeded first
    label = f"gazetted {seed.pair} {seed.period_start}..{seed.period_end} (revision)"
    if any(r.revision_of == parent.id and r.period_start == seed.period_start for r in rows):
        return SeedResult(label, "skipped")
    fx_service.publish_gazetted_rate(session, seed.to_create(revision_of=parent.id), ACTOR)
    return SeedResult(label, "created")


def seed() -> list[SeedResult]:
    """Seed every FX rate, each in its own transaction."""
    results = []
    for spot in SEED_SPOT_RATES:
        with session_scope() as session:
            results.append(seed_spot(session, spot))
    for locked in SEED_LOCKED_RATES:
        with session_scope() as session:
            results.append(seed_locked(session, locked))
    for gazetted in SEED_GAZETTED_RATES:
        with session_scope() as session:
            results.append(seed_gazetted(session, gazetted))
    return results


def main() -> None:
    for result in seed():
        print(f"{result.outcome:8} {result.label}")


if __name__ == "__main__":
    main()
