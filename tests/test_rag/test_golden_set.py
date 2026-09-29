"""Phase 9 exit criteria with the REAL models (BGE embedder + MiniLM cross-encoder).

Marked ``models``: runs locally (weights download once into the Hugging Face cache), skipped in
CI. The golden set (``tests/fixtures/rag.py``) is 12 operator-style questions against the six
seeded rules; the gate is deterministic retrieval metrics — hit rate @1 and @3 and mean
reciprocal rank — plus "nothing retrieved" for questions the rulebook cannot answer. (The
LLM-judged RAGAS scores are an opt-in report: ``python -m scripts.evaluate_rag``.)
"""

from collections.abc import Iterator
from datetime import date

import pytest
from sqlalchemy.orm import Session, sessionmaker

from models.schemas import RuleProposal
from rag import store
from scripts import seed_compliance
from services import compliance_service as svc
from tests.fixtures.rag import GOLDEN_SET, UNANSWERABLE

pytestmark = pytest.mark.models

MIN_HIT_AT_1 = 0.9
MIN_MRR = 0.9


@pytest.fixture
def seeded(session_factory: sessionmaker[Session]) -> Iterator[None]:
    seed_compliance.seed()  # approvals commit → indexed with the real embedder
    yield


def ranked_keys(session_factory: sessionmaker[Session], question: str, hs: str | None) -> list[str]:
    with session_factory() as session:
        return [m.rule.rule_key for m in svc.search_rules(session, question, hs, limit=3)]


def test_golden_set_retrieval_precision(
    seeded: None, session_factory: sessionmaker[Session]
) -> None:
    """Exit criterion (1): >=10 Q/A pairs against known approved rules score acceptably."""
    assert len(GOLDEN_SET) >= 10
    ranks = []
    misses = []
    for q in GOLDEN_SET:
        keys = ranked_keys(session_factory, q.question, q.hs_code)
        rank = keys.index(q.expected) + 1 if q.expected in keys else None
        ranks.append(rank)
        if rank != 1:
            misses.append(f"{q.question!r}: expected {q.expected}, got {keys}")

    hit_at_1 = sum(r == 1 for r in ranks) / len(ranks)
    hit_at_3 = sum(r is not None for r in ranks) / len(ranks)
    mrr = sum(1 / r for r in ranks if r is not None) / len(ranks)
    print(f"\nhit@1={hit_at_1:.3f} hit@3={hit_at_3:.3f} MRR={mrr:.3f}")
    assert hit_at_3 == 1.0, misses
    assert hit_at_1 >= MIN_HIT_AT_1, misses
    assert mrr >= MIN_MRR, misses


@pytest.mark.parametrize("question", UNANSWERABLE)
def test_unanswerable_questions_retrieve_nothing(
    seeded: None, session_factory: sessionmaker[Session], question: str
) -> None:
    """Edge case: no matching approved rule → nothing above the reranker floor."""
    assert ranked_keys(session_factory, question, None) == []


def test_pending_rule_content_is_not_retrievable(
    seeded: None, session_factory: sessionmaker[Session]
) -> None:
    """Exit criterion (2) with the real models: a question aimed at a pending version finds
    only the approved one, and the pending text is not in the collection at all."""
    pending = (
        "Parts and accessories of motor vehicles (heading 8708) attract basic customs duty of "
        "25% plus an Agriculture Infrastructure and Development Cess of 5% of the assessable "
        "value."
    )
    with session_factory() as session, session.begin():
        svc.propose_change(session, RuleProposal(
            rule_key="HSN-8708-duty", content=pending, hs_codes=["8708"],
            effective_date=date(2026, 11, 1)), "regulatory_watch_agent")  # fmt: skip

    with session_factory() as session:
        matches = svc.search_rules(session, "Is there an agriculture infrastructure cess on "
                                   "auto parts?")  # fmt: skip

    assert all(m.rule.content != pending and m.rule.version == 1 for m in matches)
    documents = store.collection().get(include=["documents"])["documents"] or []
    assert all("Agriculture Infrastructure" not in d for d in documents)
