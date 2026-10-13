"""Phase 13 — repository hygiene: no secret is tracked, private paths stay ignored."""

import pytest

from scripts import scan_secrets

# Fake credentials are assembled at run time so this file never matches its own scan.
FAKE = {
    "Groq API key": "gsk_" + "A1b2" * 8,
    "OpenAI API key": "sk-proj-" + "x9Y8" * 8,
    "Google API key": "AIza" + "S" * 35,
    "Google OAuth client secret": "GOCSPX-" + "q" * 28,
    "OAuth refresh token": '"refresh_token": "' + "1//0" + "r" * 30 + '"',
    "Private key": "-----BEGIN " + "RSA PRIVATE KEY-----",
    "Hugging Face token": "hf_" + "Z" * 34,
}


@pytest.mark.parametrize(("kind", "secret"), FAKE.items())
def test_each_credential_pattern_is_caught(kind: str, secret: str) -> None:
    [finding] = scan_secrets.scan_text("notes.md", f"config = {secret}\n")
    assert (finding.kind, finding.line) == (kind, 1)


def test_env_style_values_are_caught_but_empty_keys_and_placeholders_are_not() -> None:
    text = "POSTGRES_PASSWORD=hunter2\nGROQ_API_KEY=\nPOSTGRES_PASSWORD=ci-only-password\n"
    assert [str(f) for f in scan_secrets.scan_text(".env.example", text)] == [
        ".env.example:1: value assigned to POSTGRES_PASSWORD"
    ]
    assert scan_secrets.scan_text("guide.md", "POSTGRES_PASSWORD=hunter2") == []  # prose


def test_the_repository_is_clean() -> None:
    """The final review of Phase 13, kept as a test: nothing secret is tracked and every
    private path (``.env``, ``secrets/``, ``docs/simple_docs/``, Chroma data, outbox) is
    git-ignored."""
    assert [str(f) for f in scan_secrets.scan_tracked()] == []
    assert scan_secrets.private_paths_problems() == []
