"""The retriever's two neural models, behind small interfaces (the PolicyMind choices).

* :class:`BgeEmbedder` — ``BAAI/bge-base-en-v1.5`` sentence embeddings, L2-normalised, for the
  dense half of the hybrid search (and for what is stored in Chroma).
* :class:`CrossEncoderReranker` — ``cross-encoder/ms-marco-MiniLM-L-6-v2``, scoring each
  (question, rule) pair; its ``min_score`` is the relevance floor below which a rule is not an
  answer at all ("not found in current rulebook").

Both load lazily on first use (``sentence-transformers`` / PyTorch are the optional ``models``
extra, and the weights download once into the Hugging Face cache), and are shared process-wide
via :func:`get_embedder` / :func:`get_reranker` — which tests replace with deterministic fakes.
"""

from collections.abc import Sequence
from typing import Any, Protocol

import structlog

from config.settings import get_settings

logger = structlog.get_logger("rag")


class Embedder(Protocol):
    """Turns texts into vectors; the same model must embed the index and the queries."""

    name: str

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class Reranker(Protocol):
    """Scores how well each text answers ``query``; higher is better."""

    name: str
    min_score: float

    def score(self, query: str, texts: Sequence[str]) -> list[float]: ...


class BgeEmbedder:
    """Sentence-transformers bi-encoder (default ``BAAI/bge-base-en-v1.5``)."""

    def __init__(self, name: str) -> None:
        self.name = name
        self._model: Any = None

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if self._model is None:
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer(self.name)
        vectors = self._model.encode(list(texts), normalize_embeddings=True)
        return [[float(x) for x in row] for row in vectors]


class CrossEncoderReranker:
    """Sentence-transformers cross-encoder (default ``cross-encoder/ms-marco-MiniLM-L-6-v2``)."""

    def __init__(self, name: str, min_score: float) -> None:
        self.name = name
        self.min_score = min_score
        self._model: Any = None

    def score(self, query: str, texts: Sequence[str]) -> list[float]:
        if not texts:
            return []
        if self._model is None:
            from sentence_transformers import CrossEncoder

            self._model = CrossEncoder(self.name, max_length=512)
        scores = self._model.predict([(query, t) for t in texts], show_progress_bar=False)
        return [float(s) for s in scores]


_embedder: Embedder | None = None
_reranker: Reranker | None = None


def get_embedder() -> Embedder:
    """The process-wide embedder (configured by ``EMBEDDING_MODEL``)."""
    global _embedder
    if _embedder is None:
        _embedder = BgeEmbedder(get_settings().embedding_model)
    return _embedder


def get_reranker() -> Reranker:
    """The process-wide reranker (``RERANKER_MODEL``, floor ``RAG_MIN_RERANK_SCORE``)."""
    global _reranker
    if _reranker is None:
        settings = get_settings()
        _reranker = CrossEncoderReranker(settings.reranker_model, settings.rag_min_rerank_score)
    return _reranker


def warm_up() -> bool:
    """Load both models and run each once, now, in the calling thread. False if the optional
    ``models`` extra is not installed (the process then serves without them until first use).

    ``compliance_rulebook_mcp`` calls this at startup, **before** it starts reading MCP
    messages from stdin. On Windows, loading PyTorch/SciPy's native DLLs later — in a worker
    thread while the stdio transport has a blocking read pending on the stdin pipe — deadlocks:
    Windows serialises synchronous I/O on that handle, the DLL initialisation waits for the
    read, and the read waits for the client, which waits for this reply. Warming up first also
    takes the ~20 s cold load off the first question.
    """
    try:
        get_embedder().embed(["warm-up"])
        get_reranker().score("warm-up", ["warm-up"])
    except ImportError as exc:
        logger.warning("rag.models_unavailable", error=str(exc))
        return False
    logger.info("rag.models_ready", embedder=get_embedder().name, reranker=get_reranker().name)
    return True
