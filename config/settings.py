"""Central application settings.

All configuration is read here via pydantic-settings — never via ``os.environ[...]`` scattered
through the codebase (conventions §6). Values come from environment variables or the git-ignored
``.env`` file; empty values in ``.env`` fall back to the defaults below.
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """Typed view of the environment; one instance is shared via :func:`get_settings`."""

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
    )

    # PostgreSQL
    postgres_user: str = "customs"
    postgres_password: SecretStr
    postgres_db: str = "customs_copilot"
    postgres_test_db: str = "customs_copilot_test"
    postgres_host: str = "localhost"
    postgres_port: int = 5432

    # ChromaDB — embedded (in-process PersistentClient), no server
    chroma_path: Path = PROJECT_ROOT / "chroma_data"

    # Compliance RAG (Phase 9) — the PolicyMind models (weights download on first use, into the
    # Hugging Face cache). Hybrid retrieval: BM25 + dense → RRF → cross-encoder rerank.
    embedding_model: str = "BAAI/bge-base-en-v1.5"
    reranker_model: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"
    rag_candidates: int = 20  # fused candidates handed to the reranker
    rag_rrf_k: int = 60
    # Cross-encoder logit below which a rule is "not relevant" (so an unanswerable question gets
    # "not found in current rulebook" instead of the least-bad rule). Calibrated on the golden
    # set (2026-09-29, MiniLM): correct rules scored -2.8 .. 7.4, unanswerable questions at
    # best -10.8, wrong rules mostly below -6.4 — so the floor sits in that gap.
    rag_min_rerank_score: float = -5.0
    # Load the models when compliance_rulebook_mcp starts (required on Windows over stdio —
    # see rag.models.warm_up); tests that only launch the server switch it off.
    rag_preload_models: bool = True

    # Gmail OAuth (Phase 6)
    gmail_credentials_path: Path = PROJECT_ROOT / "secrets" / "credentials.json"
    gmail_token_path: Path = PROJECT_ROOT / "secrets" / "token.json"
    reviewer_email: str | None = None

    # Outgoing email: 'outbox' writes .eml files locally (default, no network); 'gmail' sends
    # through the Gmail API (Phase 6).
    email_backend: Literal["outbox", "gmail"] = "outbox"
    outbox_path: Path = PROJECT_ROOT / "outbox"

    # Regulatory Watch scheduler (Phase 6)
    watch_scan_interval_minutes: int = 15

    # Shipment pipeline (Phase 10): an upstream agent that has not finished after this many
    # seconds is recorded as timed out and the checklist is built without it. Generous: the
    # compliance branch waits ~25 s for its models plus several LLM calls.
    agent_timeout_seconds: float = 600.0

    # LLMs via LangChain. Two tiers per provider: reasoning (agent reasoning) and summary
    # (short plain-language text). Without the chosen provider's key, agents fall back to
    # deterministic output.
    llm_provider: Literal["groq", "openai"] = "groq"
    groq_api_key: SecretStr | None = None
    groq_reasoning_model: str = "openai/gpt-oss-120b"
    groq_summary_model: str = "openai/gpt-oss-20b"
    openai_api_key: SecretStr | None = None
    openai_reasoning_model: str = "gpt-5.5"
    openai_summary_model: str = "gpt-5.4-mini"

    log_level: str = "INFO"

    @property
    def database_url(self) -> URL:
        """SQLAlchemy URL for the psycopg (v3) driver, built from the discrete POSTGRES_* keys.

        Built rather than stored so docker-compose and the app share one set of variables.
        """
        return self._url(self.postgres_db)

    @property
    def test_database_url(self) -> URL:
        """URL of the disposable database that pytest rebuilds on every run."""
        return self._url(self.postgres_test_db)

    def _url(self, database: str) -> URL:
        return URL.create(
            drivername="postgresql+psycopg",
            username=self.postgres_user,
            password=self.postgres_password.get_secret_value(),
            host=self.postgres_host,
            port=self.postgres_port,
            database=database,
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings, loaded once on first use."""
    return Settings()
