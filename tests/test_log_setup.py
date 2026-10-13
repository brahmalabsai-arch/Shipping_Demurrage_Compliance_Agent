"""The shared structlog setup (``config.log_setup``) for servers, the scheduler and the CLIs."""

import io
import sys
from collections.abc import Iterator

import pytest
import structlog

from config.log_setup import configure_logging


@pytest.fixture
def restore_structlog() -> Iterator[None]:
    yield
    structlog.reset_defaults()


@pytest.mark.usefixtures("restore_structlog")
def test_logging_follows_stderr_even_after_the_first_stream_is_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The logger is cached on first use; it must not keep writing to the stream that was stderr
    at configure time (a test harness closes it, and every later log line then raised)."""
    first = io.StringIO()
    monkeypatch.setattr(sys, "stderr", first)
    configure_logging()
    log = structlog.get_logger("log_setup_test")
    log.info("one")
    assert '"event": "one"' in first.getvalue()

    first.close()
    second = io.StringIO()
    monkeypatch.setattr(sys, "stderr", second)
    log.info("two")

    assert '"event": "two"' in second.getvalue()
