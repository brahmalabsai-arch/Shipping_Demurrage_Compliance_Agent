"""Seed the fifteen walkthrough shipments (``docs/WALKTHROUGH.md``) and the FX locks they use.

Each shipment is one way the system can stop a shipment on its way to ``cleared``; the guide
walks each one through on the dashboards. Run it after the base seed (``python -m
scripts.seed``): the cases rely on its tariffs, exchange rates and rulebook.

Idempotent, like the other seeds: a shipment whose container already exists, or a rate already
locked for the same pair, type and date, is skipped untouched — re-running never undoes what
you did on the dashboards. To replay a case from the start, reset the database (``python -m
scripts.setup_local_db --reset``) and seed again.

    .venv\\Scripts\\python.exe -m scripts.seed_walkthrough
"""

from scripts import seed_fx_rates, seed_shipments
from services.db import session_scope
from tests.fixtures.walkthrough import WALKTHROUGH, WALKTHROUGH_LOCKS


def seed() -> list[str]:
    """Seed every walkthrough shipment and lock, each in its own transaction."""
    lines = []
    for lock in WALKTHROUGH_LOCKS:
        with session_scope() as session:
            result = seed_fx_rates.seed_locked(session, lock)
        lines.append(f"{result.outcome:8} {result.label}")
    for fixture in WALKTHROUGH:
        with session_scope() as session:
            shipment = seed_shipments.seed_one(session, fixture)
        lines.append(f"{shipment.outcome:8} {shipment.container_id}  ({shipment.key})")
    return lines


def main() -> None:
    for line in seed():
        print(line)


if __name__ == "__main__":
    main()
