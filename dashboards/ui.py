"""Presentation helpers shared by dashboard pages.

Timestamps are stored in UTC and converted to Asia/Kolkata only here, at the presentation layer,
via ``zoneinfo`` (conventions §3). Every write goes through :func:`run_action`, so each form
commits in one transaction, shows typed domain errors inline, and reruns to show fresh data.
"""

from collections.abc import Callable
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import streamlit as st
from pydantic import ValidationError
from sqlalchemy.orm import Session

from services.db import session_scope
from services.errors import CustomsCopilotError

IST = ZoneInfo("Asia/Kolkata")
_FLASH_KEY = "_flash_message"


def fmt_ist(value: datetime | None) -> str:
    """Render a stored UTC timestamp in IST, or an em dash if unknown."""
    return value.astimezone(IST).strftime("%Y-%m-%d %H:%M IST") if value else "—"


def ist_to_utc(day: date, at: time) -> datetime:
    """Interpret a date + time picked in the UI as IST and return the aware UTC instant."""
    return datetime.combine(day, at, tzinfo=IST).astimezone(ZoneInfo("UTC"))


def split_ist(value: datetime) -> tuple[date, time]:
    """Split a stored UTC timestamp into IST date and time for pre-filling inputs."""
    local = value.astimezone(IST)
    return local.date(), local.time().replace(second=0, microsecond=0)


def to_decimal(value: float | None) -> Decimal | None:
    """Convert a Streamlit number_input float to Decimal via its string form (no binary noise)."""
    return None if value is None else Decimal(str(value))


def run_action(action: Callable[[Session], Any], success: str) -> None:
    """Run one write in its own transaction; show domain/validation errors, or flash + rerun."""
    try:
        with session_scope() as session:
            action(session)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or 'input'}: {e['msg']}" for e in exc.errors()
        )
        st.error(f"Invalid input — {problems}")
    except CustomsCopilotError as exc:
        st.error(f"{type(exc).__name__}: {exc}")
    else:
        st.session_state[_FLASH_KEY] = success
        st.rerun()


def show_flash() -> None:
    """Show (once) the success message left by the previous run's action."""
    message = st.session_state.pop(_FLASH_KEY, None)
    if message:
        st.success(message)
