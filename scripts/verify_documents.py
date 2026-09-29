"""Run the Document Verification agent on shipments and print what it found (JSON).

The agent talks to its MCP servers as stdio subprocesses, exactly as in production; this script
only looks up which shipments to verify.

    .venv\\Scripts\\python.exe -m scripts.verify_documents --all
    .venv\\Scripts\\python.exe -m scripts.verify_documents --container MSCU7002002
    .venv\\Scripts\\python.exe -m scripts.verify_documents --all --as-of 2026-09-28
"""

import argparse
import asyncio
import json
from datetime import date
from typing import Any

from agents import document_verification_agent as agent
from config.log_setup import configure_logging
from models.enums import ShipmentStatus
from models.schemas import ShipmentRecord
from services import shipping_service
from services.db import session_scope
from services.errors import ShipmentCancelledError


def targets(container: str | None) -> list[ShipmentRecord]:
    """The named shipment, or every shipment that is not cancelled."""
    with session_scope() as session:
        if container:
            return [shipping_service.get_shipment_by_container_id(session, container)]
        return [s for s in shipping_service.list_shipments(session)
                if s.status is not ShipmentStatus.CANCELLED]  # fmt: skip


async def verify(shipments: list[ShipmentRecord], as_of: date | None) -> list[dict[str, Any]]:
    """One agent run per shipment; a cancelled shipment is reported, not raised."""
    report: list[dict[str, Any]] = []
    for s in shipments:
        try:
            state = await agent.verify_shipment(s.id, as_of=as_of)
        except ShipmentCancelledError as exc:
            report.append({"container_id": s.container_id, "stopped": str(exc)})
            continue
        report.append({"container_id": s.container_id,
                       **state.model_dump(mode="json", include={
                           "run_id", "as_of", "document_outcome", "document_discrepancies",
                           "fx_context", "document_explanation"})})  # fmt: skip
    return report


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m scripts.verify_documents")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--container", help="container id, e.g. MSCU7002002")
    target.add_argument("--all", action="store_true", help="every shipment that is not cancelled")
    parser.add_argument("--as-of", type=date.fromisoformat, help="analysis date (default: today)")
    args = parser.parse_args(argv)
    configure_logging()
    print(json.dumps(asyncio.run(verify(targets(args.container), args.as_of)), indent=2))


if __name__ == "__main__":
    main()
