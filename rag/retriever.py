"""Hybrid retrieval over the rulebook in force — the PolicyMind pipeline, one rule per document.

1. **Candidates** — the indexed rules, filtered to those covering ``hs_code`` (same rule as
   ``ComplianceRuleRecord.applies_to``). Only approved versions in force are ever in the index.
2. **Dense** — the query embedded with the index's model (BGE), ranked by Chroma.
3. **Sparse** — BM25 (Okapi) over the candidates' text, built per query from the collection
   itself (the rulebook is small, and the index can then never disagree with Chroma).
4. **Fusion** — Reciprocal Rank Fusion of the two rankings (``k = RAG_RRF_K``, default 60).
5. **Rerank** — the cross-encoder scores the top ``RAG_CANDIDATES`` fused rules against the
   question; rules below the reranker's ``min_score`` are dropped, so a question the rulebook
   cannot answer returns nothing (and the agent says "not found") rather than the least-bad
   rule.

PolicyMind's chunking, parent promotion and query routing are not needed: a rule is a short,
self-contained document.
"""

import re
from dataclasses import dataclass
from uuid import UUID

from rank_bm25 import BM25Okapi

from config.settings import get_settings
from models.schemas import hs_codes_cover
from rag import models, store

_WORD = re.compile(r"[a-z0-9]+")
_STOPWORDS = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "can",
        "do",
        "does",
        "for",
        "from",
        "how",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "of",
        "on",
        "or",
        "the",
        "to",
        "what",
        "when",
        "where",
        "which",
        "who",
        "with",
    ]
)


def tokenize(text: str) -> list[str]:
    """Lower-case word tokens without stop words (BM25 and the fake test models use this)."""
    return [w for w in _WORD.findall(text.lower()) if w not in _STOPWORDS]


@dataclass(frozen=True)
class Hit:
    """One retrieved rule: which version, its reranker score and the ranks behind it."""

    rule_id: UUID
    rule_key: str
    version: int
    score: float  # cross-encoder relevance (>= the reranker's min_score)
    dense_rank: int | None  # 1-based; None = not in that ranking
    bm25_rank: int | None
    text: str


def rrf(rankings: list[list[str]], k: int) -> list[tuple[str, float]]:
    """Reciprocal Rank Fusion: sum of 1 / (k + rank) over the rankings an id appears in."""
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, key in enumerate(ranking, start=1):
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda item: (-item[1], item[0]))


def search(query: str, hs_code: str | None = None, limit: int = 5) -> list[Hit]:
    """The rules in force that answer ``query`` best, most relevant first (maybe none)."""
    settings = get_settings()
    collection = store.collection()
    everything = collection.get(include=["documents", "metadatas"])
    texts = dict(zip(everything["ids"], everything["documents"] or [], strict=True))
    metas = dict(zip(everything["ids"], everything["metadatas"] or [], strict=True))
    candidates = [
        key
        for key, meta in metas.items()
        if hs_code is None or hs_codes_cover(_codes(meta), hs_code)
    ]
    if not candidates or not tokenize(query):
        return []

    dense = collection.query(
        query_embeddings=models.get_embedder().embed([query]),  # type: ignore[arg-type]
        n_results=len(texts),
        include=[],
    )["ids"][0]
    dense_ranking = [key for key in dense if key in set(candidates)]

    bm25 = BM25Okapi([tokenize(texts[key]) for key in candidates])
    bm25_scores = bm25.get_scores(tokenize(query))
    bm25_ranking = [
        key
        for key, score in sorted(zip(candidates, bm25_scores, strict=True), key=lambda p: -p[1])
        if score > 0
    ]

    fused = [key for key, _ in rrf([dense_ranking, bm25_ranking], settings.rag_rrf_k)]
    shortlist = fused[: settings.rag_candidates]
    reranker = models.get_reranker()
    scores = reranker.score(query, [texts[key] for key in shortlist])
    ranked = sorted(zip(shortlist, scores, strict=True), key=lambda p: -p[1])

    def rank_in(ranking: list[str], key: str) -> int | None:
        return ranking.index(key) + 1 if key in ranking else None

    return [
        Hit(
            rule_id=UUID(str(metas[key]["rule_id"])),
            rule_key=key,
            version=int(metas[key]["version"]),  # type: ignore[arg-type]
            score=score,
            dense_rank=rank_in(dense_ranking, key),
            bm25_rank=rank_in(bm25_ranking, key),
            text=texts[key],
        )
        for key, score in ranked
        if score >= reranker.min_score
    ][:limit]


def _codes(meta: object) -> list[str]:
    raw = str(meta.get("hs_codes", "")) if isinstance(meta, dict) else ""
    return [c for c in raw.split(",") if c]
