"""Phase 6 — LLM factory: provider and tier selection, and no model without a key.

Constructs clients only; nothing here calls a provider.
"""

from collections.abc import Callable
from typing import Any

import pytest
from langchain_groq import ChatGroq
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from agents import llm
from config.settings import Settings

Use = Callable[..., None]


@pytest.fixture
def use(monkeypatch: pytest.MonkeyPatch) -> Use:
    """Point the factory at settings built from ``values`` only (no .env)."""

    def apply(**values: Any) -> None:
        settings = Settings(_env_file=None, postgres_password=SecretStr("unused"), **values)
        monkeypatch.setattr(llm, "get_settings", lambda: settings)

    return apply


def test_groq_is_the_default_with_gpt_oss_tiers(use: Use) -> None:
    use(groq_api_key=SecretStr("gsk-test"))

    reasoning, summary = llm.reasoning_model(), llm.summary_model()

    assert isinstance(reasoning, ChatGroq) and isinstance(summary, ChatGroq)
    assert (reasoning.model_name, summary.model_name) == (
        "openai/gpt-oss-120b",
        "openai/gpt-oss-20b",
    )
    assert reasoning.reasoning_format == "parsed"  # the thinking stays out of .content


def test_openai_can_be_selected(use: Use) -> None:
    use(llm_provider="openai", openai_api_key=SecretStr("sk-test"))

    reasoning, summary = llm.reasoning_model(), llm.summary_model()

    assert isinstance(reasoning, ChatOpenAI) and isinstance(summary, ChatOpenAI)
    assert (reasoning.model_name, summary.model_name) == ("gpt-5.5", "gpt-5.4-mini")


def test_models_are_overridable_per_tier(use: Use) -> None:
    use(groq_api_key=SecretStr("gsk-test"), groq_summary_model="qwen/qwen3.8-27b")

    summary = llm.summary_model()

    assert isinstance(summary, ChatGroq) and summary.model_name == "qwen/qwen3.8-27b"


@pytest.mark.parametrize(
    "values",
    [
        {},  # groq chosen, no key
        {"groq_api_key": SecretStr("   ")},
        {"llm_provider": "openai", "groq_api_key": SecretStr("gsk-test")},  # the other key only
    ],
)
def test_no_model_without_the_chosen_providers_key(use: Use, values: dict[str, Any]) -> None:
    use(**values)

    assert llm.reasoning_model() is None and llm.summary_model() is None
