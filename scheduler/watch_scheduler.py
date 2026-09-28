"""Regulatory Watch scheduler — the only code path that captures bulletin snapshots.

Each scan (every ``WATCH_SCAN_INTERVAL_MINUTES``, or fired by hand):

1. ``bulletin_service.capture_snapshot`` — records what the bulletin says *now* (edge cases,
   Phase 6, Option A: a change made and reverted between two scans is never seen);
2. the regulatory watch agent, connected to its three MCP servers, turns a detected change into
   pending proposals and reviewer emails.

Run::

    .venv\\Scripts\\python.exe -m scheduler.watch_scheduler --once   # fire one scan now
    .venv\\Scripts\\python.exe -m scheduler.watch_scheduler          # scan on the interval

(The architecture embeds this scheduler in the FastAPI app; until that app exists it runs as
its own process with the same job.)
"""

import argparse
import asyncio
import json
from collections.abc import Callable
from uuid import uuid4

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from langchain_core.language_models import BaseChatModel

from agents import llm, regulatory_watch_agent
from agents.audit import record_run_event
from agents.mcp_clients import Connector, open_toolbox, stdio_connector
from agents.state import WatchState
from config.log_setup import configure_logging
from config.settings import get_settings
from services import bulletin_service
from services.db import session_scope
from services.errors import BulletinNotPublishedError

SCANNER = "watch_scheduler"
JOB_ID = "regulatory_watch_scan"

logger = structlog.get_logger("scheduler")


async def run_scan(
    *,
    connector: Connector | None = None,
    model_factory: Callable[[], BaseChatModel | None] | None = None,
    reviewer_email: str | None = None,
    run_id: str | None = None,
) -> WatchState | None:
    """One scan: capture, then run the agent. ``None`` if nothing has been published yet.

    Defaults: MCP servers as stdio subprocesses; the configured summary model (``None``
    without an API key → template summaries); ``REVIEWER_EMAIL``.
    """
    run_id = run_id or str(uuid4())
    try:
        with session_scope() as session:
            snapshot = bulletin_service.capture_snapshot(session, SCANNER)
    except BulletinNotPublishedError:
        logger.warning("watch.scan_skipped", run_id=run_id, reason="bulletin not published")
        return None
    logger.info("watch.snapshot_captured", run_id=run_id, seq=snapshot.seq)

    async with open_toolbox(
        regulatory_watch_agent.SERVERS,
        actor=regulatory_watch_agent.AGENT,
        run_id=run_id,
        connector=connector or stdio_connector,
    ) as toolbox:
        deps = regulatory_watch_agent.WatchDeps(
            toolbox=toolbox,
            model=(model_factory or llm.summary_model)(),
            reviewer_email=reviewer_email or get_settings().reviewer_email,
        )
        return await regulatory_watch_agent.run(deps, run_id)


async def scheduled_scan() -> None:
    """The APScheduler job: a failed scan is logged and audited, and the next one still runs."""
    run_id = str(uuid4())
    try:
        await run_scan(run_id=run_id)
    except Exception as exc:
        logger.exception("watch.scan_failed", run_id=run_id)
        try:
            record_run_event(regulatory_watch_agent.AGENT, run_id, "regulatory_watch.failed",
                             {"error": f"{type(exc).__name__}: {exc}"})  # fmt: skip
        except Exception:  # the database itself may be what failed
            logger.exception("watch.failure_not_audited", run_id=run_id)


def create_scheduler(interval_minutes: int | None = None) -> AsyncIOScheduler:
    """An asyncio scheduler with the scan job registered (one scan at a time, no pile-up)."""
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(
        scheduled_scan,
        "interval",
        minutes=interval_minutes or get_settings().watch_scan_interval_minutes,
        id=JOB_ID,
        max_instances=1,
        coalesce=True,
    )
    return scheduler


async def _serve() -> None:
    scheduler = create_scheduler()
    scheduler.start()
    logger.info("watch.scheduler_started",
                interval_minutes=get_settings().watch_scan_interval_minutes)  # fmt: skip
    await asyncio.Event().wait()  # run until interrupted


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m scheduler.watch_scheduler")
    parser.add_argument("--once", action="store_true", help="fire one scan now and exit")
    args = parser.parse_args(argv)
    configure_logging()
    if not args.once:
        asyncio.run(_serve())
        return
    state = asyncio.run(run_scan())
    print(json.dumps(None if state is None else state.model_dump(mode="json"), indent=2))


if __name__ == "__main__":
    main()
