"""Chat models for the agents, via LangChain, from the provider chosen in settings.

``LLM_PROVIDER`` picks Groq (default) or OpenAI; each has two tiers:

* ``reasoning_model()`` — multi-step agent reasoning (Groq ``openai/gpt-oss-120b``;
  OpenAI ``gpt-5.5``);
* ``summary_model()`` — short plain-language summaries (Groq ``openai/gpt-oss-20b``;
  OpenAI ``gpt-5.4-mini``).

Model names are overridable per tier in ``.env``. Both return ``None`` when the chosen
provider has no API key — callers must then use their deterministic fallback, never fail: an
LLM is a helper, not a dependency of any guarantee (proposals, audit, emails).
"""

from typing import Any, Literal

from langchain_core.language_models import BaseChatModel
from langchain_groq import ChatGroq
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from config.settings import get_settings

Tier = Literal["reasoning", "summary"]


def _usable(key: SecretStr | None) -> SecretStr | None:
    return key if key is not None and key.get_secret_value().strip() else None


def _model(tier: Tier) -> BaseChatModel | None:
    settings = get_settings()
    # Options go in as a mapping: the public keyword names are pydantic aliases mypy cannot see.
    options: dict[str, Any] = {"timeout": 60, "max_retries": 1}
    if settings.llm_provider == "groq":
        key = _usable(settings.groq_api_key)
        if key is None:
            return None
        name = settings.groq_reasoning_model if tier == "reasoning" else settings.groq_summary_model
        # gpt-oss models think before answering; keep the thinking out of ``content``.
        return ChatGroq(**{**options, "model": name, "api_key": key}, reasoning_format="parsed")
    key = _usable(settings.openai_api_key)
    if key is None:
        return None
    name = settings.openai_reasoning_model if tier == "reasoning" else settings.openai_summary_model
    return ChatOpenAI(**{**options, "model": name, "api_key": key})


def reasoning_model() -> BaseChatModel | None:
    """The model for multi-step agent reasoning, or None without the provider's API key."""
    return _model("reasoning")


def summary_model() -> BaseChatModel | None:
    """The small model for plain-language summaries, or None without the provider's API key."""
    return _model("summary")
