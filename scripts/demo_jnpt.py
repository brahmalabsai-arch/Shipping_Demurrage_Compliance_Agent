"""The JNPT auto-parts case study end to end, in one command (Phase 13) — what Mission Control
does with "Analyse all shipments" and the ROI tab, for a terminal or a script:

1. the analysis pipeline for every shipment that is not cancelled (document verification →
   demurrage & risk ‖ compliance → checklist), one ``run_id`` each, over stdio MCP servers;
2. an ROI snapshot of the same data (default assumptions unless given) and its before/after
   comparison.

Re-runnable without a reset: every run has its own run ids and its own snapshot (a timestamped
name), so two demos never share state.

    .venv\\Scripts\\python.exe -m scripts.demo_jnpt                    # today's IST date
    .venv\\Scripts\\python.exe -m scripts.demo_jnpt --as-of 2026-09-28  # the documented figures
"""

import argparse
import asyncio
import json
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal

from langchain_core.language_models import BaseChatModel
from pydantic import BaseModel

from agents import graph
from agents.ist import today_ist
from agents.mcp_clients import Connector
from agents.state import CaseState
from config.log_setup import configure_logging
from models.enums import ShipmentStatus
from models.schemas import RoiAssumptions
from roi.calculator import RoiComparison, compare
from services import roi_service, shipping_service
from services.db import session_scope


class DemoShipment(BaseModel):
    """One shipment's analysis, as the demo reports it."""

    container_id: str
    run_id: str
    documents: list[str]  # discrepancy codes; [] = clear
    risk: str | None
    demurrage_inr: Decimal | None
    rulebook_answered: int
    rulebook_not_found: int
    checklist: str | None
    ready_for_clearance: bool | None
    case: CaseState


class DemoReport(BaseModel):
    """Everything one run of the demo produced."""

    as_of: date
    shipments: list[DemoShipment]
    snapshot_name: str
    snapshot_sha256: str
    roi: RoiComparison


def _summary(case: CaseState) -> DemoShipment:
    estimate = case.demurrage_estimate
    found = sum(f.found for f in case.compliance_findings)
    return DemoShipment(
        container_id=case.shipment.container_id if case.shipment else str(case.shipment_id),
        run_id=case.run_id,
        documents=[d.code for d in case.document_discrepancies],
        risk=case.risk,
        demurrage_inr=estimate.amount_inr if estimate else None,
        rulebook_answered=found,
        rulebook_not_found=len(case.compliance_findings) - found,
        checklist=case.checklist.summary if case.checklist else None,
        ready_for_clearance=case.checklist.ready_for_clearance if case.checklist else None,
        case=case,
    )


async def run_demo(
    *,
    as_of: date | None = None,
    connector: Connector | None = None,
    model_factory: Callable[[], BaseChatModel | None] | None = None,
    snapshot_name: str | None = None,
    assumptions: RoiAssumptions | None = None,
    actor: str = "jnpt-demo",
) -> DemoReport:
    """Analyse every shipment, then freeze and compare an ROI snapshot of the same data."""
    as_of = as_of or today_ist()
    with session_scope() as session:
        shipments = [s for s in shipping_service.list_shipments(session)
                     if s.status is not ShipmentStatus.CANCELLED]  # fmt: skip
    analysed = [
        _summary(await graph.analyse_shipment(s.id, connector=connector,
                                              model_factory=model_factory, as_of=as_of))
        for s in shipments
    ]  # fmt: skip
    name = snapshot_name or f"jnpt-demo {as_of} {datetime.now(UTC):%Y%m%dT%H%M%S%fZ}"
    with session_scope() as session:
        snapshot = roi_service.capture_snapshot(
            session, name, as_of, assumptions or RoiAssumptions(), actor
        )
    return DemoReport(
        as_of=as_of,
        shipments=analysed,
        snapshot_name=snapshot.name,
        snapshot_sha256=snapshot.content_sha256,
        roi=compare(snapshot, snapshot),
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m scripts.demo_jnpt")
    parser.add_argument("--as-of", type=date.fromisoformat, help="analysis date (default today)")
    parser.add_argument("--name", help="ROI snapshot name (default: timestamped)")
    args = parser.parse_args(argv)
    configure_logging()
    report = asyncio.run(run_demo(as_of=args.as_of, snapshot_name=args.name))
    print(json.dumps({
        "as_of": report.as_of.isoformat(),
        "shipments": [s.model_dump(mode="json", exclude={"case"}) for s in report.shipments],
        "roi_snapshot": {"name": report.snapshot_name, "sha256": report.snapshot_sha256},
        "roi_avoided_inr": str(report.roi.total_avoided_inr),
        "roi_lines": {line.container_id: str(line.avoided_inr) for line in report.roi.lines},
    }, indent=2))  # fmt: skip


if __name__ == "__main__":
    main()
