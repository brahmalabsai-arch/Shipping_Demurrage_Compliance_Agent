"""Phase 9 — the Compliance agent (RAG) over MCP (in-memory servers), with the fake retrieval
models (conftest) and fake LLMs. The real-model retrieval quality gate is tests/test_rag.

Edge cases: an unanswerable question gets exactly "Not found in current rulebook." with no
fabricated code or rate (and no LLM call); a rule with an approved and a newer pending version
is only ever answered from the approved one. The LLM may only phrase what was retrieved: an
uncited, mis-cited or number-inventing reply is replaced by the quoted rule.
"""

import re
from datetime import date
from typing import Any
from uuid import UUID

import pytest
import structlog
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from agents import compliance_rag_agent as agent
from agents import graph
from agents.mcp_clients import memory_connector
from agents.state import Discrepancy
from models.db_models import AuditEvent
from models.schemas import RuleProposal
from scripts import seed_compliance, seed_fx_rates, seed_port_rates, seed_shipments
from services import compliance_service, shipping_service
from tests.fixtures.rag import UNANSWERABLE
from tests.fixtures.shipments import QTY_MISMATCH

INVOICE_QUESTION = "Can customs assess the bill of entry before the commercial invoice is filed?"
INVOICE_RULE = "DOC-commercial-invoice-required v1"


class ExplodingModel(FakeListChatModel):
    async def _agenerate(self, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("the model must not be called")


@pytest.fixture
def rulebook(session_factory: sessionmaker[Session]) -> None:
    seed_compliance.seed()  # approvals commit → indexed


async def ask(question: str, model: Any = None, hs_code: str | None = None) -> Any:
    return await agent.ask(question, hs_code, connector=memory_connector,
                           model_factory=lambda: model)  # fmt: skip


# --------------------------------------------------------------------------- edge cases


@pytest.mark.parametrize("question", UNANSWERABLE)
async def test_unanswerable_question_is_not_found_and_nothing_is_invented(
    rulebook: None, question: str
) -> None:
    finding = await ask(question, model=ExplodingModel(responses=[""]))

    assert (finding.found, finding.answer, finding.citations) == (False, agent.NOT_FOUND, [])
    assert not re.search(r"\d", finding.answer)  # no HS code, rate or date made up


async def test_pending_version_is_never_the_answer(
    rulebook: None, session_factory: sessionmaker[Session]
) -> None:
    """Edge case: HSN-8708-duty v1 approved (15%), v2 pending (25% + a cess) → the answer
    rests on v1 only."""
    with session_factory() as session, session.begin():
        compliance_service.propose_change(session, RuleProposal(
            rule_key="HSN-8708-duty", hs_codes=["8708"], effective_date=date(2026, 11, 1),
            content="Parts of heading 8708 attract basic customs duty of 25% plus an "
                    "Agriculture Infrastructure and Development Cess of 5%."), "watch")  # fmt: skip

    finding = await ask("What basic customs duty applies to parts of heading 8708?",
                        hs_code="87084000")  # fmt: skip

    assert finding.found
    assert {(c.rule_key, c.version) for c in finding.citations if c.rule_key == "HSN-8708-duty"} \
        == {("HSN-8708-duty", 1)}  # fmt: skip
    assert "15%" in finding.answer and "25%" not in finding.answer
    assert "Agriculture" not in finding.answer


# --------------------------------------------------------------------------- answers


async def test_without_an_llm_the_best_rule_is_quoted_with_its_citation(rulebook: None) -> None:
    finding = await ask(INVOICE_QUESTION)

    assert finding.found and finding.method == "template"
    assert finding.fallback_reason == "no LLM configured"
    assert finding.citations[0].rule_key == "DOC-commercial-invoice-required"
    assert finding.answer.startswith(f"Per [{INVOICE_RULE}] (effective 2026-01-01): A bill of "
                                     "entry cannot be assessed")  # fmt: skip


async def test_a_cited_grounded_llm_answer_is_used(rulebook: None) -> None:
    text = (f"No. A bill of entry cannot be assessed without the commercial invoice, and "
            f"demurrage accrues to the importer until it is filed [{INVOICE_RULE}].")  # fmt: skip

    finding = await ask(INVOICE_QUESTION, model=FakeListChatModel(responses=[text]))

    assert (finding.method, finding.answer, finding.fallback_reason) == ("llm", text, None)


@pytest.mark.parametrize(
    ("replies", "reason"),
    [
        (["It cannot be assessed [DOC-made-up-rule v1]."], "cited rules that were not retrieved"),
        (["It cannot be assessed without the invoice."], "cited no rule"),
        (
            [
                "File it within 30 days [DOC-commercial-invoice-required v1].",
                "File it within 30 days [DOC-commercial-invoice-required v1].",
            ],
            "numbers not in the texts",
        ),
    ],
)
async def test_an_ungrounded_llm_answer_falls_back_to_the_quoted_rule(
    rulebook: None, replies: list[str], reason: str
) -> None:
    finding = await ask(INVOICE_QUESTION, model=FakeListChatModel(responses=replies))

    assert finding.method == "template" and reason in (finding.fallback_reason or "")
    assert finding.answer.startswith(f"Per [{INVOICE_RULE}]")


async def test_the_llm_may_judge_the_retrieved_rules_irrelevant(rulebook: None) -> None:
    finding = await ask(INVOICE_QUESTION, model=FakeListChatModel(responses=[agent.NOT_FOUND]))

    assert (finding.found, finding.answer, finding.method) == (False, agent.NOT_FOUND, "llm")


async def test_a_question_is_answered_through_the_rulebook_server_only_and_audited(
    rulebook: None, session_factory: sessionmaker[Session]
) -> None:
    with structlog.testing.capture_logs() as logs:
        await ask(INVOICE_QUESTION)

    calls = [(e["server"], e["tool"]) for e in logs if e["event"] == "agent.tool_call"]
    assert calls == [("compliance_rulebook_mcp", "search_rules")]
    assert agent.SERVERS == ("compliance_rulebook_mcp",)
    with session_factory() as session:
        [event] = session.scalars(
            select(AuditEvent).where(AuditEvent.action == "compliance_rag.answered")).all()  # fmt: skip
    assert event.actor == agent.AGENT
    assert event.payload["citations"][0] == INVOICE_RULE


# --------------------------------------------------------------------------- the shipment pipeline


@pytest.fixture
def seeded(session_factory: sessionmaker[Session]) -> None:
    seed_shipments.seed()
    seed_port_rates.seed()
    seed_fx_rates.seed()
    seed_compliance.seed()


def shipment_id(session_factory: sessionmaker[Session], container_id: str) -> UUID:
    with session_factory() as session:
        return shipping_service.get_shipment_by_container_id(session, container_id).id


async def test_pipeline_answers_the_shipment_questions_with_citations(
    seeded: None, session_factory: sessionmaker[Session]
) -> None:
    """Korean gearboxes with a quantity mismatch: the rulebook is asked about duty,
    documents, valuation, the Korea preference and the mismatch — each answer cited."""
    state = await graph.analyse_shipment(shipment_id(session_factory, "MSCU7002002"),
                                         connector=memory_connector, model_factory=lambda: None,
                                         as_of=date(2026, 9, 28))  # fmt: skip

    by_question = {f.question: f for f in state.compliance_findings}
    top = {q: (f.citations[0].rule_key if f.citations else None) for q, f in by_question.items()}
    quantity = agent.DISCREPANCY_QUESTIONS["quantity_mismatch"]
    assert top[quantity] == "DOC-quantity-consistency"
    korea = next(q for q in top if "originating in Korea" in q)
    assert top[korea] == "ORG-KR-CEPA"
    assert by_question[korea].hs_code == "87084000"
    duty = next(q for q in top if q.startswith("What basic customs duty"))
    assert top[duty] == "HSN-8708-duty"
    assert all(f.agent == agent.AGENT for f in state.compliance_findings)
    # the parallel branch ran too
    assert state.risk == "high" and state.demurrage_estimate is not None
    with session_factory() as session:
        actions = set(session.scalars(
            select(AuditEvent.action).where(AuditEvent.entity_id == UUID(state.run_id))))  # fmt: skip
    assert actions == {"document_verification.completed", "demurrage_risk.completed",
                       "compliance_rag.completed"}  # fmt: skip


def test_shipment_questions_are_fixed_and_follow_the_findings() -> None:
    record: Any = QTY_MISMATCH.shipment  # has the fields the questions use (HS, cargo, origin)
    found = [Discrepancy(code="quantity_mismatch", field="qty", message="m", agent="x")] * 2

    questions = agent.shipment_questions(record, found)

    assert [q for q, _ in questions].count(agent.DISCREPANCY_QUESTIONS["quantity_mismatch"]) == 1
    assert (
        "What basic customs duty, surcharge and IGST apply to HS code 87084000?",
        "87084000",
    ) in questions
    assert any("originating in Korea" in q for q, _ in questions)
    assert len(questions) == 6
