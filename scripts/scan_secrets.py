"""Repository hygiene check (Phase 13): nothing secret or private is tracked by git.

A small, dependency-free stand-in for ``git-secrets``, run by the test suite and CI:

* every tracked file (``git ls-files``) is scanned for credential patterns — Groq/OpenAI/
  Google API keys, OAuth client secrets and refresh tokens, private keys, and non-empty
  ``*_PASSWORD`` / ``*_API_KEY`` / ``*_SECRET`` / ``*_TOKEN`` assignments in env-style files;
* the paths conventions §6 says must never be committed (``.env``, ``secrets/``,
  ``docs/simple_docs/``, the Chroma data and the email outbox) are tracked by nothing, and are
  git-ignored.

    .venv\\Scripts\\python.exe -m scripts.scan_secrets      # exit code 1 if anything is found
"""

import re
import subprocess
import sys
from dataclasses import dataclass

from config.settings import PROJECT_ROOT

# Paths that must never be tracked, and must be ignored (a sample file inside each).
PRIVATE_PATHS = (".env", "secrets/token.json", "docs/simple_docs/notes.md", "chroma_data/x",
                 "outbox/x.eml")  # fmt: skip

PATTERNS: dict[str, re.Pattern[str]] = {
    "Groq API key": re.compile(r"\bgsk_[A-Za-z0-9]{20,}"),
    "OpenAI API key": re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}"),
    "Google API key": re.compile(r"\bAIza[0-9A-Za-z_-]{30,}"),
    "Google OAuth client secret": re.compile(r"\bGOCSPX-[0-9A-Za-z_-]{20,}"),
    "OAuth refresh token": re.compile(r'"refresh_token"\s*:\s*"[^"]{10,}"'),
    "Private key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "Hugging Face token": re.compile(r"\bhf_[A-Za-z0-9]{30,}"),
}
# KEY=value lines in env-style files: a value after a secret-sounding key is a leak.
ENV_ASSIGNMENT = re.compile(
    r"^\s*(?P<key>[A-Z0-9_]*(?:PASSWORD|API_KEY|SECRET|TOKEN))\s*=\s*(?P<value>\S+)"
)
# Placeholders that are fine in docs, CI and examples.
ALLOWED_VALUES = {"ci-only-password", "<password>", "your-password", "changeme", "..."}
SKIP_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".lock")


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    kind: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.kind}"


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=PROJECT_ROOT, capture_output=True, text=True,
                          check=True, encoding="utf-8").stdout  # fmt: skip


def tracked_files() -> list[str]:
    return [p for p in _git("ls-files", "-z").split("\0") if p]


def scan_text(path: str, text: str) -> list[Finding]:
    """Secret-looking content in one file's text."""
    findings: list[Finding] = []
    env_style = path.endswith((".env", ".env.example", ".yml", ".yaml", ".toml", ".ini", ".cfg"))
    for number, line in enumerate(text.splitlines(), start=1):
        for kind, pattern in PATTERNS.items():
            if pattern.search(line):
                findings.append(Finding(path, number, kind))
        match = ENV_ASSIGNMENT.match(line) if env_style else None
        if match and match["value"].strip("'\"") not in ALLOWED_VALUES:
            findings.append(Finding(path, number, f"value assigned to {match['key']}"))
    return findings


def scan_tracked() -> list[Finding]:
    """Secret-looking content anywhere in the tracked files (this file's own patterns aside)."""
    findings: list[Finding] = []
    for path in tracked_files():
        if path.endswith(SKIP_SUFFIXES) or path == "scripts/scan_secrets.py":
            continue
        try:
            text = (PROJECT_ROOT / path).read_text(encoding="utf-8")
        except (UnicodeDecodeError, FileNotFoundError):
            continue
        findings += scan_text(path, text)
    return findings


def private_paths_problems() -> list[str]:
    """Private paths that are tracked, or would not be ignored if created."""
    tracked = set(tracked_files())
    problems = [f"{p} is tracked" for p in sorted(tracked)
                if p == ".env" or p.startswith(("secrets/", "docs/simple_docs/",
                                                "chroma_data/", "outbox/"))]  # fmt: skip
    for sample in PRIVATE_PATHS:
        ignored = subprocess.run(["git", "check-ignore", "-q", sample], cwd=PROJECT_ROOT,
                                 check=False).returncode == 0  # fmt: skip
        if not ignored:
            problems.append(f"{sample} is not git-ignored")
    return problems


def main() -> int:
    findings = scan_tracked()
    problems = private_paths_problems()
    for finding in findings:
        print(f"SECRET? {finding}")
    for problem in problems:
        print(f"PRIVATE PATH: {problem}")
    if findings or problems:
        return 1
    print(f"OK: {len(tracked_files())} tracked files scanned; private paths are ignored.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
