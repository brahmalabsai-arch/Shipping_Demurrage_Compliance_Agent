"""Guarded LLM prose: a model may rephrase the facts it is given, never add figures to them.

Every agent that uses an LLM does so only for text a human reads (a change summary, an
explanation of findings), always with a deterministic fallback (architecture §6). This module is
the shared guard: a reply that quotes a number absent from the source facts — typically a
computed difference or percentage — gets one corrective retry naming the offending numbers; if
the rewrite still has one, or the call fails or returns nothing, the caller gets a reason
instead of text and uses its template.
"""

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage

_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")


def numbers_in(text: str) -> set[Decimal]:
    """Every number written in ``text`` (thousands separators dropped), for comparison."""
    values = set()
    for token in _NUMBER.findall(text):
        try:
            values.add(Decimal(token.replace(",", "")))
        except InvalidOperation:  # pragma: no cover - the regex only yields digit groups
            continue
    return values


@dataclass(frozen=True)
class GuardedReply:
    """Either the model's text, or why it must not be used (exactly one is set)."""

    text: str | None
    fallback_reason: str | None = None


async def guarded_reply(
    model: BaseChatModel, system: str, prompt: str, source: str
) -> GuardedReply:
    """Ask ``model``; accept the reply only if every number in it also appears in ``source``."""
    messages: list[BaseMessage] = [SystemMessage(system), HumanMessage(prompt)]
    allowed = numbers_in(source)
    invented: list[Decimal] = []
    for attempt in (1, 2):
        try:
            reply = await model.ainvoke(messages)
        except Exception as exc:  # any provider failure (quota, network, timeout) -> template
            return GuardedReply(None, f"LLM call failed: {type(exc).__name__}")
        text = " ".join((reply.content if isinstance(reply.content, str) else "").split())
        if not text:
            return GuardedReply(None, "LLM returned no text")
        invented = sorted(numbers_in(text) - allowed)
        if not invented:
            return GuardedReply(text)
        if attempt == 1:
            listed = ", ".join(str(n) for n in invented)
            messages += [
                reply,
                HumanMessage(
                    f"Your reply contains {listed}, which appear in neither text you were given "
                    "— probably a calculation. Rewrite it quoting only figures exactly as "
                    "written, with no arithmetic."
                ),
            ]
    return GuardedReply(
        None, f"LLM reply contained numbers not in the texts (after one retry): {invented}"
    )
