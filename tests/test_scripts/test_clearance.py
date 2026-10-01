"""Phase 11 — ``python -m scripts.clearance``: the supervisor's functions from the command line."""

import json
from datetime import date
from typing import Any

import pytest
from sqlalchemy.orm import Session, sessionmaker

from agents import supervisor
from agents.mcp_clients import memory_connector
from scripts import clearance, seed_compliance, seed_fx_rates, seed_port_rates, seed_shipments


def run_cli(capsys: pytest.CaptureFixture[str], *argv: str) -> Any:
    clearance.main(list(argv))
    return json.loads(capsys.readouterr().out)


def test_start_list_show_and_approve(
    session_factory: sessionmaker[Session],
    checkpoints: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(supervisor, "stdio_connector", memory_connector)
    seed_shipments.seed()
    seed_port_rates.seed()
    seed_fx_rates.seed()
    seed_compliance.seed()
    capsys.readouterr()  # the seeds' log lines

    started = run_cli(capsys, "start", "--container", "MSKU7001001", "--as-of", "2026-09-28")
    run_id = started["run"]["run_id"]
    assert started["run"]["state"] == "awaiting_filing"
    assert started["run"]["started_by"] == "clearance-cli"
    assert [w["gate"] for w in started["waiting_on"]] == ["filing"]
    assert started["case"]["as_of"] == date(2026, 9, 28).isoformat()

    [listed] = run_cli(capsys, "list", "--open")
    assert listed["run_id"] == run_id
    assert run_cli(capsys, "show", run_id)["case"]["checklist"]["ready_for_clearance"] is True

    filed = run_cli(capsys, "approve", run_id, "--reviewer", "Asha Rao")
    assert filed["run"]["state"] == "filed"
    assert filed["case"]["filing_decision"]["reviewer"] == "Asha Rao"
    assert run_cli(capsys, "list", "--open") == []
