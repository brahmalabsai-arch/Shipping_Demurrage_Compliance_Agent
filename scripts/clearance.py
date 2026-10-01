"""Supervised clearance runs from the command line (Phase 11) — the same functions the Shipping
Ops page calls. Runs are checkpointed in Postgres, so each command may be a separate process:
start a run here, decide the rule on the Compliance page, resume it here, approve it there.

    .venv\\Scripts\\python.exe -m scripts.clearance start --container MSKU7001001
    .venv\\Scripts\\python.exe -m scripts.clearance list --open
    .venv\\Scripts\\python.exe -m scripts.clearance show <run_id>
    .venv\\Scripts\\python.exe -m scripts.clearance resume-compliance <run_id> --rule-id <uuid>
    .venv\\Scripts\\python.exe -m scripts.clearance approve <run_id> --reviewer "Rupin"
    .venv\\Scripts\\python.exe -m scripts.clearance reject <run_id> --reviewer "Rupin" --note "..."
    .venv\\Scripts\\python.exe -m scripts.clearance abandon <run_id> --reviewer "Rupin" --reason "..."

Output is JSON: the run (state, what it waits on) and the case summary.
"""

import argparse
import asyncio
import json
from datetime import date
from typing import Any
from uuid import UUID

from agents import supervisor
from config.log_setup import configure_logging
from services import governance_service, shipping_service
from services.db import session_scope

SLICE = {"as_of", "risk", "data_gaps", "checklist", "compliance_decisions", "filing_decision",
         "outcome", "outcome_detail"}  # fmt: skip


def report(run: supervisor.ClearanceRun) -> dict[str, Any]:
    return {
        "run": run.run.model_dump(mode="json"),
        "waiting_on": run.waiting_on,
        "case": run.case.model_dump(mode="json", include=SLICE),
    }


async def dispatch(args: argparse.Namespace) -> Any:
    if args.command == "start":
        with session_scope() as session:
            sid = shipping_service.get_shipment_by_container_id(session, args.container).id
        return report(await supervisor.start_clearance_run(sid, started_by=args.by,
                                                           as_of=args.as_of))  # fmt: skip
    if args.command == "list":
        with session_scope() as session:
            of = (shipping_service.get_shipment_by_container_id(session, args.container).id
                  if args.container else None)  # fmt: skip
            runs = governance_service.list_runs(session, of, open_only=args.open)
        return [r.model_dump(mode="json") for r in runs]
    if args.command == "show":
        return report(await supervisor.inspect_run(args.run_id))
    if args.command == "resume-compliance":
        return report(await supervisor.resume_after_compliance_decision(args.run_id, args.rule_id))
    if args.command in ("approve", "reject"):
        return report(await supervisor.resume_after_filing_approval(
            args.run_id, args.command, args.reviewer, args.note))  # fmt: skip
    if args.command == "abandon":
        run = supervisor.abandon_clearance_run(args.run_id, args.reviewer, args.reason)
        return run.model_dump(mode="json")
    raise AssertionError(args.command)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m scripts.clearance")
    commands = parser.add_subparsers(dest="command", required=True)

    start = commands.add_parser("start", help="start a supervised clearance run")
    start.add_argument("--container", required=True, help="container id, e.g. MSKU7001001")
    start.add_argument("--as-of", type=date.fromisoformat, help="analysis date (default: today)")
    start.add_argument("--by", default="clearance-cli", help="who starts it (audit)")

    runs = commands.add_parser("list", help="clearance runs, newest first")
    runs.add_argument("--container")
    runs.add_argument("--open", action="store_true", help="only runs still waiting")

    commands.add_parser("show", help="a run's checkpointed state").add_argument("run_id")

    resume = commands.add_parser("resume-compliance",
                                 help="resume after the rule was decided (Compliance page)")  # fmt: skip
    resume.add_argument("run_id")
    resume.add_argument("--rule-id", type=UUID, required=True)

    for verb in ("approve", "reject"):
        decide = commands.add_parser(verb, help=f"{verb} the filing (registered reviewer)")
        decide.add_argument("run_id")
        decide.add_argument("--reviewer", required=True)
        decide.add_argument("--note", required=verb == "reject")

    abandon = commands.add_parser("abandon", help="end an open run by hand (failed)")
    abandon.add_argument("run_id")
    abandon.add_argument("--reviewer", required=True)
    abandon.add_argument("--reason", required=True)

    args = parser.parse_args(argv)
    configure_logging()
    print(json.dumps(asyncio.run(dispatch(args)), indent=2))


if __name__ == "__main__":
    main()
