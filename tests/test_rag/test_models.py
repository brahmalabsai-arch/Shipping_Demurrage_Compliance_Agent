"""Model warm-up: compliance_rulebook_mcp loads the models before it reads stdin (Windows)."""

from typing import Any

import pytest

from config.settings import get_settings
from mcp_servers.compliance_rulebook_mcp import server
from rag import models


class _Missing:
    name = "missing"
    min_score = 0.0

    def embed(self, texts: Any) -> Any:
        raise ImportError("No module named 'sentence_transformers'")


def test_warm_up_runs_both_models_once() -> None:
    assert models.warm_up() is True  # the conftest fakes stand in for the real models


def test_warm_up_without_the_models_extra_is_not_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(models, "get_embedder", lambda: _Missing())

    assert models.warm_up() is False


@pytest.mark.parametrize("preload", [True, False])
def test_the_rulebook_server_warms_up_before_serving(
    monkeypatch: pytest.MonkeyPatch, preload: bool
) -> None:
    calls: list[str] = []

    def warm_up() -> bool:
        calls.append("warm_up")
        return True

    monkeypatch.setattr(get_settings(), "rag_preload_models", preload)
    monkeypatch.setattr(models, "warm_up", warm_up)  # the module the server calls
    monkeypatch.setattr(server.mcp, "run", lambda: calls.append("run"))

    server.main()

    assert calls == (["warm_up", "run"] if preload else ["run"])
