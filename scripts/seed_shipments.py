"""Seed the four JNPT case-study shipments (clean / qty-mismatch / missing-doc / delayed).

Idempotent: a shipment whose container_id already exists is skipped untouched — so re-running
never duplicates rows and never overwrites edits made on the dashboard since the last seed.
Each shipment is created through the service layer and walked through its real lifecycle
transitions, so the audit trail reads like a genuine history.

    .venv\\Scripts\\python.exe -m scripts.seed_shipments
"""

from dataclasses import dataclass
from typing import Literal

from sqlalchemy.orm import Session

from services import shipping_service
from services.db import session_scope
from services.errors import ShipmentNotFoundError
from tests.fixtures.shipments import SEED_SHIPMENTS, FixtureShipment

ACTOR = "seed_script"


@dataclass(frozen=True)
class SeedResult:
    container_id: str
    key: str
    outcome: Literal["created", "skipped"]


def seed_one(session: Session, fixture: FixtureShipment) -> SeedResult:
    """Create one fixture shipment with its documents and status history, unless present."""
    container_id = fixture.shipment.container_id
    try:
        shipping_service.get_shipment_by_container_id(session, container_id)
        return SeedResult(container_id, fixture.key, "skipped")
    except ShipmentNotFoundError:
        pass

    shipment = shipping_service.create_shipment(session, fixture.shipment, ACTOR)
    for document in fixture.documents:
        shipping_service.add_document(session, shipment.id, document, ACTOR)
    for status in fixture.status_path:
        shipping_service.update_status(session, shipment.id, status, ACTOR)
    return SeedResult(container_id, fixture.key, "created")


def seed() -> list[SeedResult]:
    """Seed every fixture, each in its own transaction so one failure can't block the rest."""
    results = []
    for fixture in SEED_SHIPMENTS:
        with session_scope() as session:
            results.append(seed_one(session, fixture))
    return results


def main() -> None:
    for result in seed():
        print(f"{result.outcome:8} {result.container_id}  ({result.key})")


if __name__ == "__main__":
    main()
