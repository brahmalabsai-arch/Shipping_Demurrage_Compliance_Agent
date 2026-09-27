"""Seed the demurrage tariffs (INNSA base + 2026-10-01 revision, INMUN base).

Idempotent: a tariff period that already starts on the same day for the same port and container
type is skipped untouched — re-running never duplicates periods and never overwrites dashboard
corrections.

    .venv\\Scripts\\python.exe -m scripts.seed_port_rates
"""

from dataclasses import dataclass
from typing import Literal

from sqlalchemy.orm import Session

from models.schemas import PortRateUpsert
from services import demurrage_service
from services.db import session_scope
from tests.fixtures.port_rates import SEED_PORT_RATES

ACTOR = "seed_script"


@dataclass(frozen=True)
class SeedResult:
    label: str
    outcome: Literal["created", "skipped"]


def seed_one(session: Session, rate: PortRateUpsert) -> SeedResult:
    """Add one tariff period unless a period for the same line already starts that day."""
    label = f"{rate.port_code} {rate.container_type} from {rate.effective_from}"
    existing = demurrage_service.list_rates(session, rate.port_code)
    if any(
        r.container_type == rate.container_type and r.effective_from == rate.effective_from
        for r in existing
    ):
        return SeedResult(label, "skipped")
    demurrage_service.upsert_rate(session, rate, ACTOR)
    return SeedResult(label, "created")


def seed() -> list[SeedResult]:
    """Seed every tariff period, each in its own transaction."""
    results = []
    for rate in SEED_PORT_RATES:
        with session_scope() as session:
            results.append(seed_one(session, rate))
    return results


def main() -> None:
    for result in seed():
        print(f"{result.outcome:8} {result.label}")


if __name__ == "__main__":
    main()
