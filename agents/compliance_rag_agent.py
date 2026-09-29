"""Compliance agent (RAG) — answers customs questions from the **approved** rulebook only, with
citations (problem statement FR5; implementation plan Phase 9; edge cases, Phase 9).

Through ``compliance_rulebook_mcp`` only (conventions §4: this agent never touches shipping
data through MCP — it reads the shipment from ``CaseState``, written by document
verification):

1. The questions for a shipment are fixed, not generated: the duty and special requirements
   for its HS code, the documents needed for assessment, the valuation exchange rate, any
   origin preference, plus one question per kind of document discrepancy found.
2. ``search_rules`` — hybrid retrieval over the RAG index, which only ever holds approved
   versions in force (pending content is never embedded). No hit above the reranker's floor
   → the answer is exactly "Not found in current rulebook." — no LLM call, nothing guessed.
3. The LLM (reasoning tier) answers from the retrieved rules only and must cite them as
   ``[<rule_key> v<version>]``. Its reply is rejected — and the rule text is quoted instead —
   if it cites a rule that was not retrieved, cites none, contains a number that is in neither
   the question nor the rules (:mod:`agents.guarded_llm`), fails, or no model is configured.
   It may still judge the retrieved rules irrelevant and say "Not found in current rulebook."

The agent changes no data; each run is audited (``compliance_rag.completed`` for a shipment,
``compliance_rag.answered`` for a single question).

State slice: reads ``CaseState.run_id``, ``shipment`` and ``document_discrepancies``; writes
``compliance_findings``.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import structlog
from langchain_core.language_models import BaseChatModel
from langchain_core.runnables import RunnableConfig
from pydantic import TypeAdapter

from agents import llm
from agents.audit import record_run_event
from agents.guarded_llm import guarded_reply
from agents.mcp_clients import Connector, McpToolbox, open_toolbox, stdio_connector
from agents.state import CaseState, Citation, ComplianceFinding, Discrepancy
from models.schemas import RuleMatch, ShipmentRecord

AGENT = "compliance_rag_agent"
RULEBOOK = "compliance_rulebook_mcp"
SERVERS = (RULEBOOK,)

NOT_FOUND = "Not found in current rulebook."
MAX_RULES = 3  # retrieved rules handed to the LLM per question

ANSWER_PROMPT = (
    "You answer customs compliance questions for an Indian importer using ONLY the approved "
    "rules provided. Cite every rule you rely on by its key and version in square brackets, "
    "exactly as given, e.g. [HSN-8708-duty v1]. Quote rates, dates and codes exactly as "
    "written; never add a number that is not in the rules or the question, and do not "
    "calculate anything (duties and surcharges compound). If the rules provided do not answer "
    f"the question, reply exactly: {NOT_FOUND} Answer in 1-3 plain sentences, no markdown."
)

COUNTRIES = {"CN": "China", "KR": "Korea", "JP": "Japan", "DE": "Germany", "US": "the USA"}

DISCREPANCY_QUESTIONS: dict[str, str] = {
    "quantity_mismatch": "What happens when the quantities on the commercial invoice, packing "
    "list and bill of lading do not agree?",
    "missing_document": "Can goods be assessed and cleared if the commercial invoice has not "
    "been filed?",
    "value_mismatch": "Which value is assessed when the invoice value differs from the declared "
    "value?",
    "currency_mismatch": "Which exchange rate converts an invoice in a foreign currency into "
    "rupees?",
    "invalid_currency": "Which exchange rate converts an invoice in a foreign currency into "
    "rupees?",
    "date_mismatch": "Which date decides the exchange rate used to value imported goods?",
    "quote_expired": "Which date decides the exchange rate used to value imported goods?",
    "fx_rate_unavailable": "Which exchange rate converts an invoice in a foreign currency into "
    "rupees?",
}

_CITATION = re.compile(r"\[([A-Za-z0-9]+(?:-[A-Za-z0-9]+)*) v(\d+)\]")

logger = structlog.get_logger("agents")


@dataclass(frozen=True)
class ComplianceDeps:
    """What the node needs besides state (``config["configurable"][AGENT]``)."""

    toolbox: McpToolbox
    model: BaseChatModel | None


def shipment_questions(
    shipment: ShipmentRecord, discrepancies: list[Discrepancy]
) -> list[tuple[str, str | None]]:
    """The fixed questions for one shipment, as (question, HS code filter), de-duplicated."""
    hs = shipment.hs_code
    cargo = shipment.cargo_type.lower()
    questions: list[tuple[str, str | None]] = [
        (f"What basic customs duty, surcharge and IGST apply to HS code {hs}?", hs),
        (f"What special requirements apply to importing {cargo} (HS code {hs})?", hs),
        ("Which documents must be filed before the bill of entry can be assessed?", None),
        ("Which exchange rate converts the invoice value into rupees for assessment?", None),
    ]
    country = COUNTRIES.get(shipment.origin_port[:2], shipment.origin_port[:2])
    questions.append((f"Is preferential duty available for goods of HS code {hs} originating "
                      f"in {country}, and what does it require?", hs))  # fmt: skip
    for code in sorted({d.code for d in discrepancies}):
        if code in DISCREPANCY_QUESTIONS:
            questions.append((DISCREPANCY_QUESTIONS[code], None))
    unique: dict[str, str | None] = {}
    for question, hs_code in questions:
        unique.setdefault(question, hs_code)
    return list(unique.items())


def _citation(match: RuleMatch) -> Citation:
    rule = match.rule
    return Citation(rule_id=rule.id, rule_key=rule.rule_key, version=rule.version,
                    effective_date=rule.effective_date, score=match.score)  # fmt: skip


def _label(match: RuleMatch) -> str:
    return f"{match.rule.rule_key} v{match.rule.version}"


def template_answer(matches: list[RuleMatch]) -> str:
    """The deterministic answer: the best rule quoted verbatim, others named."""
    best = matches[0].rule
    text = f"Per [{_label(matches[0])}] (effective {best.effective_date}): {best.content}"
    if len(matches) > 1:
        text += " Also relevant: " + ", ".join(f"[{_label(m)}]" for m in matches[1:]) + "."
    return text


def _rules_context(matches: list[RuleMatch]) -> str:
    return "\n\n".join(
        f"[{_label(m)}] effective {m.rule.effective_date}, HS codes "
        f"{', '.join(m.rule.hs_codes) or 'all'}:\n{m.rule.content}"
        for m in matches
    )


def _citation_problem(text: str, matches: list[RuleMatch]) -> str | None:
    cited = {f"{key} v{version}" for key, version in _CITATION.findall(text)}
    allowed = {_label(m) for m in matches}
    if not cited:
        return "LLM answer cited no rule"
    if cited - allowed:
        return f"LLM answer cited rules that were not retrieved: {sorted(cited - allowed)}"
    return None


async def answer_question(
    tools: McpToolbox, model: BaseChatModel | None, question: str, hs_code: str | None = None
) -> ComplianceFinding:
    """Retrieve from the approved rulebook and answer with citations — or say not found."""
    raw = await tools.call(RULEBOOK, "search_rules",
                           {"query": question, "hs_code": hs_code, "limit": MAX_RULES})  # fmt: skip
    matches = TypeAdapter(list[RuleMatch]).validate_python(raw)

    def finding(**kw: Any) -> ComplianceFinding:
        return ComplianceFinding(question=question, hs_code=hs_code, agent=AGENT, **kw)

    if not matches:
        return finding(found=False, answer=NOT_FOUND, method="template")
    citations = [_citation(m) for m in matches]
    fallback = template_answer(matches)
    if model is None:
        return finding(found=True, answer=fallback, citations=citations, method="template",
                       fallback_reason="no LLM configured")  # fmt: skip
    context = _rules_context(matches)
    prompt = f"Question: {question}\n\nApproved rules:\n{context}"
    reply = await guarded_reply(model, ANSWER_PROMPT, prompt, f"{question}\n{context}")
    if reply.text is not None and reply.text.strip() == NOT_FOUND:
        return finding(found=False, answer=NOT_FOUND, citations=citations, method="llm")
    problem = reply.fallback_reason or _citation_problem(reply.text or "", matches)
    if problem is not None:
        return finding(found=True, answer=fallback, citations=citations, method="template",
                       fallback_reason=problem)  # fmt: skip
    return finding(found=True, answer=reply.text, citations=citations, method="llm")


def _summary(findings: list[ComplianceFinding]) -> list[dict[str, Any]]:
    return [{"question": f.question, "found": f.found, "method": f.method,
             "fallback_reason": f.fallback_reason,
             "citations": [f"{c.rule_key} v{c.version}" for c in f.citations]}
            for f in findings]  # fmt: skip


# --------------------------------------------------------------------------- the node


async def compliance_rag_agent(state: CaseState, config: RunnableConfig) -> dict[str, Any]:
    """LangGraph node: the shipment's fixed questions → grounded, cited answers."""
    deps: ComplianceDeps = config["configurable"][AGENT]
    shipment = state.shipment
    if shipment is None:  # nothing to ask about: document verification did not run first
        logger.warning("agent.no_shipment_in_state", agent=AGENT, run_id=state.run_id)
        return {"compliance_findings": []}
    findings = [
        await answer_question(deps.toolbox, deps.model, question, hs_code)
        for question, hs_code in shipment_questions(shipment, state.document_discrepancies)
    ]
    record_run_event(AGENT, state.run_id, "compliance_rag.completed", {
        "shipment_id": str(shipment.id), "container_id": shipment.container_id,
        "findings": _summary(findings)})  # fmt: skip
    logger.info("agent.run_completed", agent=AGENT, run_id=state.run_id,
                container_id=shipment.container_id, questions=len(findings),
                not_found=sum(not f.found for f in findings))  # fmt: skip
    return {"compliance_findings": findings}


async def ask(
    question: str,
    hs_code: str | None = None,
    *,
    connector: Connector | None = None,
    model_factory: Callable[[], BaseChatModel | None] | None = None,
    run_id: str | None = None,
) -> ComplianceFinding:
    """Answer one free-form question from the approved rulebook (dashboard / CLI)."""
    run_id = run_id or str(uuid4())
    async with open_toolbox(
        SERVERS, actor=AGENT, run_id=run_id, connector=connector or stdio_connector
    ) as toolbox:
        result = await answer_question(
            toolbox, (model_factory or llm.reasoning_model)(), question, hs_code
        )
    record_run_event(AGENT, run_id, "compliance_rag.answered", _summary([result])[0])
    return result
