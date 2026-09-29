"""Deterministic stand-ins for the retriever's neural models, and the RAG golden set.

Most tests run with :class:`FakeEmbedder` / :class:`FakeReranker` (installed by the autouse
fixture in ``tests/conftest.py``): lexical, instant, offline — enough to exercise indexing,
filtering, fusion and the relevance floor. Tests marked ``models`` use the real BGE embedder
and MiniLM cross-encoder instead (the golden-set quality gate).
"""

import math
import zlib
from collections.abc import Sequence
from dataclasses import dataclass

from rag.retriever import tokenize


class FakeEmbedder:
    """Hashed bag of words, L2-normalised: cosine similarity = weighted word overlap."""

    name = "fake-hashed-bow"
    dimensions = 512

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        vectors = []
        for text in texts:
            vector = [0.0] * self.dimensions
            for token in tokenize(text):
                vector[zlib.crc32(token.encode()) % self.dimensions] += 1.0
            norm = math.sqrt(sum(x * x for x in vector)) or 1.0
            vectors.append([x / norm for x in vector])
        return vectors


class FakeReranker:
    """Share of the question's distinct words that the rule contains (0..1)."""

    name = "fake-word-overlap"
    min_score = 0.25

    def score(self, query: str, texts: Sequence[str]) -> list[float]:
        wanted = set(tokenize(query))
        return [len(wanted & set(tokenize(t))) / len(wanted) if wanted else 0.0 for t in texts]


# --------------------------------------------------------------------------- golden set


@dataclass(frozen=True)
class GoldenQuestion:
    """A question an importer would ask, and the rule that answers it."""

    question: str
    expected: str  # rule_key
    hs_code: str | None = None


# Phrased the way an operator asks, deliberately not copying the rule text word for word.
# fmt: off
GOLDEN_SET: tuple[GoldenQuestion, ...] = (
    GoldenQuestion("What basic customs duty is charged on motor vehicle parts?", "HSN-8708-duty"),
    GoldenQuestion("How much IGST applies to imported auto parts?", "HSN-8708-duty"),
    GoldenQuestion("Is a social welfare surcharge levied on gearboxes?", "HSN-8708-duty",
                   "87084000"),
    GoldenQuestion("Do imported brake assemblies need a safety certificate?",
                   "HSN-870830-brake-certification", "87083000"),
    GoldenQuestion("What happens to brake parts shipped without a conformity certificate?",
                   "HSN-870830-brake-certification"),
    GoldenQuestion("The packing list shows fewer units than the invoice. What now?",
                   "DOC-quantity-consistency"),
    GoldenQuestion("Must the bill of lading quantity match the other documents?",
                   "DOC-quantity-consistency"),
    GoldenQuestion("Can customs assess our shipment before the commercial invoice is filed?",
                   "DOC-commercial-invoice-required"),
    GoldenQuestion("Who pays demurrage if the invoice is late when the vessel arrives?",
                   "DOC-commercial-invoice-required"),
    GoldenQuestion("Which exchange rate converts the invoice value into rupees?",
                   "VAL-exchange-rate"),
    GoldenQuestion("If customs revises the notified exchange rate, which rate applies?",
                   "VAL-exchange-rate"),
    GoldenQuestion("How do we claim the lower duty for auto parts made in Korea?",
                   "ORG-KR-CEPA", "87088000"),
)
# fmt: on

# Questions the seeded rulebook cannot answer: retrieval must return nothing.
UNANSWERABLE: tuple[str, ...] = (
    "What is the anti-dumping duty on solar panels from Vietnam?",
    "Which licence is needed to import live poultry?",
    "How long can a warehouse bond be extended for liquor?",
)
