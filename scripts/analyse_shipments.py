"""Run the shipment pipeline (document verification → demurrage & risk and compliance) and print
the results.

The agents talk to their MCP servers as stdio subprocesses, exactly as in production; this
script only looks up which shipments to analyse.

    .venv\\Scripts\\python.exe -m scripts.analyse_shipments --all
    .venv\\Scripts\\python.exe -m scripts.analyse_shipments --container HLXU7004004 --as-of 2026-09-28
"""

import argparse
import asyncio
import json
from datetime import date
from typing import Any

from agents import graph
from config.log_setup import configure_logging
from scripts.verify_documents import targets
from services.errors import ShipmentCancelledError

SLICE = {
    "run_id", "as_of", "document_outcome", "document_discrepancies", "risk", "risk_reasons",
    "data_gaps", "demurrage_estimate", "compliance_findings",
}  # fmt: skip


async def analyse(container: str | None, as_of: date | None) -> list[dict[str, Any]]:
    """One pipeline run per shipment; a cancelled shipment is reported, not raised."""
    report: list[dict[str, Any]] = []
    for s in targets(container):
        try:
            state = await graph.analyse_shipment(s.id, as_of=as_of)
        except ShipmentCancelledError as exc:
            report.append({"container_id": s.container_id, "stopped": str(exc)})
            continue
        report.append({"container_id": s.container_id,
                       **state.model_dump(mode="json", include=SLICE)})  # fmt: skip
    return report


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m scripts.analyse_shipments")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--container", help="container id, e.g. HLXU7004004")
    target.add_argument("--all", action="store_true", help="every shipment that is not cancelled")
    parser.add_argument("--as-of", type=date.fromisoformat, help="analysis date (default: today)")
    args = parser.parse_args(argv)
    configure_logging()
    print(json.dumps(asyncio.run(analyse(args.container, args.as_of)), indent=2))


if __name__ == "__main__":
    main()
