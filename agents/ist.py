"""Business calendar days for the agents: timestamps are stored in UTC, but a lifecycle "day"
(an arrival, the analysis date) is the day in India (IST), as everywhere else in the system."""

from datetime import date, datetime
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def today_ist() -> date:
    """The business calendar day now — the default analysis date of a live agent run."""
    return datetime.now(IST).date()


def ist_day(moment: datetime) -> date:
    """The IST calendar day of a stored (aware, UTC) timestamp."""
    return moment.astimezone(IST).date()
