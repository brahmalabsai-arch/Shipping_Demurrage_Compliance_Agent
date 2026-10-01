"""ROI snapshots and before/after comparisons from the command line (Phase 12) — the same
functions as Mission Control → ROI.

    .venv\\Scripts\\python.exe -m scripts.roi capture --name fixtures --as-of 2026-09-28
    .venv\\Scripts\\python.exe -m scripts.roi capture --name slow --base-days 4 --hold missing_document=10
    .venv\\Scripts\\python.exe -m scripts.roi list
    .venv\\Scripts\\python.exe -m scripts.roi compare --before fixtures [--after other]

Output is JSON: a comparison has the totals and, per shipment, every figure (issues, stay,
free/chargeable days, daily rate, tariff row, FX rate and row, INR amounts, the basis).
"""

import argparse
import json
from datetime import date
from typing import Any

from agents.ist import today_ist
from models.schemas import DEFAULT_HOLD_DAYS, RoiAssumptions
from roi.calculator import compare
from services import roi_service
from services.db import session_scope


def _holds(pairs: list[str]) -> dict[str, int]:
    holds = dict(DEFAULT_HOLD_DAYS)
    for pair in pairs:
        code, _, days = pair.partition("=")
        holds[code.strip()] = int(days)
    return holds


def dispatch(args: argparse.Namespace) -> Any:
    if args.command == "capture":
        assumptions = RoiAssumptions(base_clearance_days=args.base_days,
                                     hold_days=_holds(args.hold), combine=args.combine)  # fmt: skip
        with session_scope() as session:
            snapshot = roi_service.capture_snapshot(
                session, args.name, args.as_of or today_ist(), assumptions, args.by, args.note
            )
        return {"id": str(snapshot.id), "name": snapshot.name, "as_of": snapshot.as_of.isoformat(),
                "content_sha256": snapshot.content_sha256,
                "gaps": {s.shipment.container_id: s.gaps
                         for s in snapshot.content.shipments if s.gaps}}  # fmt: skip
    if args.command == "list":
        with session_scope() as session:
            snapshots = roi_service.list_snapshots(session)
        return [{"name": s.name, "as_of": s.as_of.isoformat(), "captured_by": s.captured_by,
                 "captured_at": s.captured_at.isoformat(), "content_sha256": s.content_sha256}
                for s in snapshots]  # fmt: skip
    if args.command == "compare":
        with session_scope() as session:
            before = roi_service.get_snapshot_by_name(session, args.before)
            after = roi_service.get_snapshot_by_name(session, args.after or args.before)
        return compare(before, after).model_dump(mode="json")
    raise AssertionError(args.command)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m scripts.roi")
    commands = parser.add_subparsers(dest="command", required=True)

    capture = commands.add_parser("capture", help="freeze the live data into a named snapshot")
    capture.add_argument("--name", required=True)
    capture.add_argument("--as-of", type=date.fromisoformat, help="analysis date (default today)")
    capture.add_argument("--base-days", type=int, default=3, help="days in port, papers in order")
    capture.add_argument("--hold", action="append", default=[], metavar="CODE=DAYS",
                         help="override a hold duration, e.g. missing_document=10")  # fmt: skip
    capture.add_argument("--combine", choices=["max", "sum"], default="max")
    capture.add_argument("--note")
    capture.add_argument("--by", default="roi-cli", help="who captures it (audit)")

    commands.add_parser("list", help="snapshots, newest first")

    comparison = commands.add_parser("compare", help="before/after ROI between two snapshots")
    comparison.add_argument("--before", required=True, help="snapshot name (agents disabled)")
    comparison.add_argument("--after", help="snapshot name (agents enabled); default = before")

    print(json.dumps(dispatch(parser.parse_args(argv)), indent=2))


if __name__ == "__main__":
    main()
