"""Phase 9 — what reaches the RAG index, and when (deterministic fake models; see conftest).

The guarantee under test: only the approved version *in force* of a rule is ever embedded —
pending, rejected and superseded text never enters Chroma, an approval is indexed only after
its transaction commits, and a stale entry is never served. The exit criterion's "pending
content is absent from the collection entirely" is asserted on the raw Chroma documents.
"""

from collections.abc import Iterator
from datetime import date
from uuid import uuid4

import numpy as np
import pytest
from sqlalchemy.orm import Session, sessionmaker

from models.enums import RuleDecision
from models.schemas import ComplianceRuleRecord, RuleProposal
from rag import ingest, retriever, store
from scripts import seed_compliance
from services import compliance_service as svc
from services.errors import RuleNotIndexableError
from tests.fixtures.compliance import BASELINE_RULE_KEYS
from tests.fixtures.rag import UNANSWERABLE

KEY = "HSN-8708-duty"
ANY_VECTOR = np.full((1, 512), 0.1, dtype=np.float32)  # for entries written by hand
REVIEWER = "a.sharma (compliance)"
# A proposal whose wording appears nowhere in the approved rulebook.
PENDING_TEXT = (
    "Parts and accessories of motor vehicles (heading 8708) attract basic customs duty of 25% "
    "plus an Agriculture Infrastructure and Development Cess of 5% of the assessable value."
)


@pytest.fixture
def seeded(session_factory: sessionmaker[Session]) -> Iterator[None]:
    seed_compliance.seed()
    yield


def indexed() -> dict[str, tuple[str, dict[str, object]]]:
    everything = store.collection().get(include=["documents", "metadatas"])
    return {
        key: (doc, dict(meta))
        for key, doc, meta in zip(
            everything["ids"],
            everything["documents"] or [],
            everything["metadatas"] or [],
            strict=True,
        )
    }


def propose(session_factory: sessionmaker[Session], content: str) -> ComplianceRuleRecord:
    with session_factory() as session, session.begin():
        return svc.propose_change(session, RuleProposal(
            rule_key=KEY, content=content, hs_codes=["8708"],
            effective_date=date(2026, 11, 1)), "regulatory_watch_agent")  # fmt: skip


def search_keys(session_factory: sessionmaker[Session], query: str) -> list[str]:
    with session_factory() as session:
        return [m.rule.rule_key for m in svc.search_rules(session, query)]


# --------------------------------------------------------------------------- indexing


def test_seeded_approvals_are_indexed_once_committed(seeded: None) -> None:
    entries = indexed()

    assert set(entries) == set(BASELINE_RULE_KEYS)
    doc, meta = entries[KEY]
    assert doc.startswith(f"{KEY}\nParts and accessories of motor vehicles")
    assert (meta["version"], meta["hs_codes"]) == (1, "8708")
    assert indexed()["VAL-exchange-rate"][1]["hs_codes"] == ""  # all goods


def test_pending_content_is_never_embedded(
    seeded: None, session_factory: sessionmaker[Session]
) -> None:
    """Exit criterion (2): a question aimed at a pending rule's content finds no match, and
    that content is absent from the Chroma collection itself — not filtered after retrieval."""
    propose(session_factory, PENDING_TEXT)

    matches = search_keys(session_factory, "agriculture infrastructure development cess 25%")

    assert all(PENDING_TEXT not in doc for doc, _ in indexed().values())
    assert all("Agriculture Infrastructure" not in doc for doc, _ in indexed().values())
    assert indexed()[KEY][1]["version"] == 1
    with session_factory() as session:
        found = svc.search_rules(session, "agriculture infrastructure development cess 25%")
    assert all(m.rule.content != PENDING_TEXT for m in found)
    assert matches == [] or matches == [KEY]  # at most the approved v1, never v2


def test_approving_the_new_version_replaces_the_old_entry(
    seeded: None, session_factory: sessionmaker[Session]
) -> None:
    v1_text = indexed()[KEY][0]
    v2 = propose(session_factory, PENDING_TEXT)

    with session_factory() as session, session.begin():
        svc.decide(session, v2.id, RuleDecision.APPROVE, REVIEWER)

    doc, meta = indexed()[KEY]
    assert (meta["version"], meta["rule_id"]) == (2, str(v2.id))
    assert PENDING_TEXT in doc
    assert all(v1_text != d for d, _ in indexed().values())  # superseded text left the index
    assert search_keys(session_factory, "agriculture infrastructure development cess") == [KEY]


def test_a_rejected_proposal_is_never_indexed(
    seeded: None, session_factory: sessionmaker[Session]
) -> None:
    v2 = propose(session_factory, PENDING_TEXT)

    with session_factory() as session, session.begin():
        svc.decide(session, v2.id, RuleDecision.REJECT, REVIEWER, "not notified yet")

    assert indexed()[KEY][1]["version"] == 1
    assert all("Agriculture Infrastructure" not in doc for doc, _ in indexed().values())


def test_an_approval_that_rolls_back_is_not_indexed(
    seeded: None, session_factory: sessionmaker[Session]
) -> None:
    v2 = propose(session_factory, PENDING_TEXT)

    with pytest.raises(RuntimeError), session_factory() as session, session.begin():
        svc.decide(session, v2.id, RuleDecision.APPROVE, REVIEWER)
        raise RuntimeError("the surrounding work failed after the decision")

    assert indexed()[KEY][1]["version"] == 1
    with session_factory() as session:
        assert svc.get_current_rule(session, KEY).version == 1  # the decision did not happen


def test_index_rule_refuses_anything_but_the_version_in_force(
    seeded: None, session_factory: sessionmaker[Session]
) -> None:
    pending = propose(session_factory, PENDING_TEXT)

    with pytest.raises(RuleNotIndexableError, match="pending"):
        ingest.index_rule(pending)
    assert all(PENDING_TEXT not in doc for doc, _ in indexed().values())


def test_a_stale_entry_is_never_served(
    seeded: None, session_factory: sessionmaker[Session]
) -> None:
    """Defence in depth: even if pending text got into Chroma by another route, search_rules
    re-reads every hit from Postgres and drops what is not approved and in force."""
    pending = propose(session_factory, PENDING_TEXT)
    store.collection().upsert(  # bypassing ingest on purpose
        ids=["ROGUE-ENTRY"], documents=[PENDING_TEXT],
        embeddings=ANY_VECTOR,
        metadatas=[{"rule_id": str(pending.id), "rule_key": "ROGUE-ENTRY", "version": 2,
                    "hs_codes": "8708", "effective_date": "2026-11-01"}],
    )  # fmt: skip

    assert "ROGUE-ENTRY" in {h.rule_key for h in retriever.search("agriculture cess 25%")}
    assert "ROGUE-ENTRY" not in search_keys(session_factory, "agriculture cess 25%")


def test_sync_makes_the_index_exactly_the_rulebook_in_force(
    seeded: None, session_factory: sessionmaker[Session]
) -> None:
    collection = store.collection()
    collection.delete(ids=["VAL-exchange-rate"])
    collection.upsert(ids=["OLD-RULE"], documents=["obsolete"], embeddings=ANY_VECTOR,
                      metadatas=[{"rule_id": str(uuid4()), "rule_key": "OLD-RULE", "version": 1,
                                  "hs_codes": "", "effective_date": "2025-01-01"}])  # fmt: skip

    with session_factory() as session:
        report = svc.sync_rag_index(session)

    assert set(indexed()) == set(BASELINE_RULE_KEYS)
    assert report.removed == ["OLD-RULE"]
    assert f"{KEY} v1" in report.indexed


# --------------------------------------------------------------------------- retrieval


def test_hs_code_filter_matches_the_rulebook_reads(
    seeded: None, session_factory: sessionmaker[Session]
) -> None:
    hits = {h.rule_key for h in retriever.search("certificate for brake parts", "87084000")}

    assert "HSN-870830-brake-certification" not in hits  # 870830 does not cover gearboxes
    assert "HSN-870830-brake-certification" in {
        h.rule_key for h in retriever.search("certificate for brake parts", "87083000")
    }


@pytest.mark.parametrize("question", UNANSWERABLE)
def test_an_unanswerable_question_retrieves_nothing(seeded: None, question: str) -> None:
    assert retriever.search(question) == []


def test_every_hit_explains_its_ranks(seeded: None) -> None:
    [top, *_] = retriever.search("Korean certificate of origin preferential duty", "87084000")

    assert top.rule_key == "ORG-KR-CEPA"
    assert top.dense_rank == 1 and top.bm25_rank == 1
    assert top.score >= 0.25


def test_rrf_sums_reciprocal_ranks() -> None:
    fused = retriever.rrf([["a", "b", "c"], ["b", "c"]], k=60)

    assert [key for key, _ in fused] == ["b", "c", "a"]
    assert fused[0][1] == pytest.approx(1 / 62 + 1 / 61)
