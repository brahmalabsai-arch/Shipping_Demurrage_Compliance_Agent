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
