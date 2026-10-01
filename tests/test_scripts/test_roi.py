"""Phase 12 — ``python -m scripts.roi``: capture, list and compare from the command line."""

import json
from typing import Any

import pytest
from sqlalchemy.orm import Session, sessionmaker

from scripts import roi, seed_compliance, seed_fx_rates, seed_port_rates, seed_shipments


def run_cli(capsys: pytest.CaptureFixture[str], *argv: str) -> Any:
    roi.main(list(argv))
    return json.loads(capsys.readouterr().out)


def test_capture_list_and_compare(
    session_factory: sessionmaker[Session], capsys: pytest.CaptureFixture[str]
) -> None:
    seed_shipments.seed()
    seed_port_rates.seed()
    seed_fx_rates.seed()
    seed_compliance.seed()
    capsys.readouterr()  # the seeds' log lines

    captured = run_cli(capsys, "capture", "--name", "fixtures", "--as-of", "2026-09-28")
    run_cli(capsys, "capture", "--name", "long holds", "--as-of", "2026-09-28",
            "--hold", "missing_document=10")  # fmt: skip
    assert captured["gaps"] == {}
    assert [s["name"] for s in run_cli(capsys, "list")] == ["long holds", "fixtures"]

    result = run_cli(capsys, "compare", "--before", "fixtures")
    assert result["total_avoided_inr"] == "82212.00"
    assert result["before_snapshot"]["content_sha256"] == captured["content_sha256"]
    longer = run_cli(capsys, "compare", "--before", "long holds", "--after", "fixtures")
    assert longer["total_avoided_inr"] == "111384.00"  # 23,868.00 + 87,516.00
