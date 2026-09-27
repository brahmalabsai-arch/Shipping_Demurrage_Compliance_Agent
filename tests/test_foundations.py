"""Phase 0 smoke tests: configuration loading and the secrets-hygiene conventions (§6)."""

import pytest
from pydantic import SecretStr

from config.settings import PROJECT_ROOT, Settings


def _env_example_keys() -> dict[str, str]:
    keys: dict[str, str] = {}
    for raw in (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            key, _, value = line.partition("=")
            keys[key] = value
    return keys


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the developer's shell environment from leaking into Settings under test."""
    for key in _env_example_keys():
        monkeypatch.delenv(key, raising=False)


@pytest.mark.usefixtures("clean_env")
def test_settings_builds_psycopg_database_url() -> None:
    settings = Settings(_env_file=None, postgres_password=SecretStr("s3cret"), postgres_port=5433)

    url = settings.database_url

    assert url.drivername == "postgresql+psycopg"
    assert (url.username, url.host, url.port, url.database) == (
        "customs",
        "localhost",
        5433,
        "customs_copilot",
    )
    assert url.password == "s3cret"
    assert "s3cret" not in repr(settings)  # SecretStr keeps it out of logs


@pytest.mark.usefixtures("clean_env")
def test_empty_env_values_fall_back_to_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    # A freshly copied .env.example has every key present but empty.
    monkeypatch.setenv("POSTGRES_PASSWORD", "x")
    monkeypatch.setenv("POSTGRES_PORT", "")
    monkeypatch.setenv("CHROMA_PATH", "")

    settings = Settings(_env_file=None)

    assert settings.postgres_port == 5432
    assert settings.chroma_path == PROJECT_ROOT / "chroma_data"


def test_env_example_lists_every_setting_with_no_values() -> None:
    keys = _env_example_keys()

    assert {k: v for k, v in keys.items() if v} == {}, ".env.example must hold keys only"
    assert set(keys) == {name.upper() for name in Settings.model_fields}


@pytest.mark.parametrize("entry", [".env", "secrets/", "docs/simple_docs/"])
def test_gitignore_protects_secrets_and_private_docs(entry: str) -> None:
    lines = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()

    assert entry in {line.strip() for line in lines}
