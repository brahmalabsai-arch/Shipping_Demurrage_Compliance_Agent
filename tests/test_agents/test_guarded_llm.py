"""The shared guard on LLM prose: no figure may appear that the source facts do not contain."""

from decimal import Decimal
from typing import Any

from langchain_core.language_models.fake_chat_models import FakeListChatModel

from agents.guarded_llm import guarded_reply, numbers_in

SOURCE = "Invoice qty 1,200 vs packing list 1,150 at USD/INR 87.73."


class ExplodingModel(FakeListChatModel):
    async def _agenerate(self, *args: Any, **kwargs: Any) -> Any:
        raise TimeoutError("provider timed out")


def test_numbers_are_read_with_thousands_separators_dropped() -> None:
    assert numbers_in(SOURCE) == {Decimal("1200"), Decimal("1150"), Decimal("87.73")}


async def test_a_reply_quoting_only_source_figures_is_accepted() -> None:
    model = FakeListChatModel(responses=["Packing list shows 1150, invoice 1200."])

    reply = await guarded_reply(model, "system", "prompt", SOURCE)

    assert (reply.text, reply.fallback_reason) == ("Packing list shows 1150, invoice 1200.", None)


async def test_one_corrective_retry_then_accept() -> None:
    model = FakeListChatModel(responses=["It is 50 units short.", "It is short of the invoice."])

    reply = await guarded_reply(model, "system", "prompt", SOURCE)

    assert reply.text == "It is short of the invoice."


async def test_invented_number_after_the_retry_is_rejected() -> None:
    model = FakeListChatModel(responses=["50 units short.", "Still 50 units short."])

    reply = await guarded_reply(model, "system", "prompt", SOURCE)

    assert reply.text is None
    assert reply.fallback_reason == (
        "LLM reply contained numbers not in the texts (after one retry): [Decimal('50')]"
    )


async def test_provider_failure_and_empty_reply_are_reasons_not_errors() -> None:
    failed = await guarded_reply(ExplodingModel(responses=[""]), "s", "p", SOURCE)
    empty = await guarded_reply(FakeListChatModel(responses=["  "]), "s", "p", SOURCE)

    assert failed.fallback_reason == "LLM call failed: TimeoutError"
    assert empty.fallback_reason == "LLM returned no text"
