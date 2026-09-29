"""Seed FX rates covering every lifecycle date of the four JNPT case-study shipments.

Pairs are the fixture currencies into INR (1 unit of base = ``rate`` INR). All values are
synthetic. What each table covers:

* spot     — one 13:30 IST observation per pair on each quote date (07-20, 08-10, 08-12, 09-01)
             and on the arrival/dock dates (09-08, 09-20, 09-22, 10-05), so a port-charges lookup
             on those days visibly ignores the (different) spot rate.
* locked   — BoL rates on the on-board dates (EUR 08-01, USD 08-22 and 08-25, JPY 09-10) and
             commercial-invoice rates on the invoice dates (EUR 07-28, USD 08-18 and 08-20). The
             missing-document shipment has no invoice, so JPY has no commercial-invoice lock.
* gazetted — fortnightly customs notifications July-October 2026 (CBIC notifies roughly twice a
             month), plus two corrections: EUR/INR 09-01..09-15 revised for the whole period
             (a lookup for the already-delayed shipment's EUR goods, docked 09-08, must return
             the revision), and USD/INR 09-16..09-30 revised from 09-21 only (a lookup on 09-20
             → original; from 09-21 → revision). Demurrage tariffs are in USD, so the risk
             agent converts them at USD/INR on the analysis date (Phase 8: 2026-09-28 → 88.40).
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

from models.enums import LockType
from models.schemas import GazettedRateCreate, LockedRateCreate, SpotRateUpsert

USD, EUR, JPY = "USD/INR", "EUR/INR", "JPY/INR"
SPOT_SOURCE = "simulated-spot-feed"
AUTHORITY = "CBIC (simulated)"


def _d(month: int, day: int) -> date:
    return date(2026, month, day)


# ---------------------------------------------------------------------------- spot

SPOT_DAYS = (_d(7, 20), _d(8, 10), _d(8, 12), _d(9, 1), _d(9, 8), _d(9, 20), _d(9, 22), _d(10, 5))
_SPOT_VALUES: dict[str, tuple[str, ...]] = {
    USD: ("87.4200", "87.6150", "87.7300", "88.0400", "88.1900", "88.3550", "88.4100", "88.6200"),
    EUR: (
        "101.8500",
        "102.3100",
        "102.2400",
        "102.9000",
        "103.1200",
        "103.4000",
        "103.3700",
        "103.8800",
    ),
    JPY: ("0.5921", "0.5948", "0.5953", "0.5987", "0.6002", "0.6015", "0.6011", "0.6034"),
}


def spot_instant(day: date) -> datetime:
    """13:30 IST (08:00 UTC) — the time every seeded spot observation is taken."""
    return datetime(day.year, day.month, day.day, 8, 0, tzinfo=UTC)


SEED_SPOT_RATES: tuple[SpotRateUpsert, ...] = tuple(
    SpotRateUpsert(pair=pair, rate=Decimal(value), as_of=spot_instant(day), source=SPOT_SOURCE)
    for pair, values in _SPOT_VALUES.items()
    for day, value in zip(SPOT_DAYS, values, strict=True)
)

# ---------------------------------------------------------------------------- locked


def _lock(pair: str, lock_type: LockType, day: date, rate: str) -> LockedRateCreate:
    return LockedRateCreate(pair=pair, rate=Decimal(rate), lock_date=day, lock_type=lock_type)


BOL, CI = LockType.BOL, LockType.COMMERCIAL_INVOICE

SEED_LOCKED_RATES: tuple[LockedRateCreate, ...] = (
    _lock(EUR, CI, _d(7, 28), "101.9700"),  # already_delayed invoice
    _lock(EUR, BOL, _d(8, 1), "102.0500"),  # already_delayed on board
    _lock(USD, CI, _d(8, 18), "87.7050"),  # clean invoice
    _lock(USD, CI, _d(8, 20), "87.7800"),  # qty_mismatch invoice
    _lock(USD, BOL, _d(8, 22), "87.8600"),  # clean on board
    _lock(USD, BOL, _d(8, 25), "87.9400"),  # qty_mismatch on board
    _lock(JPY, BOL, _d(9, 10), "0.6004"),  # missing_document on board
)

# ---------------------------------------------------------------------------- gazetted


@dataclass(frozen=True)
class GazettedSeed:
    """A gazetted row to publish. A revision names the row it corrects by that row's
    ``period_start`` (its id is only known once seeded)."""

    pair: str
    rate: Decimal
    period_start: date
    period_end: date
    revises_period_start: date | None = None

    def to_create(self, revision_of: UUID | None = None) -> GazettedRateCreate:
        return GazettedRateCreate(
            pair=self.pair,
            rate=self.rate,
            period_start=self.period_start,
            period_end=self.period_end,
            publishing_authority=AUTHORITY,
            revision_of=revision_of,
        )


FORTNIGHTS: tuple[tuple[date, date], ...] = (
    (_d(7, 1), _d(7, 15)),
    (_d(7, 16), _d(7, 31)),
    (_d(8, 1), _d(8, 15)),
    (_d(8, 16), _d(8, 31)),
    (_d(9, 1), _d(9, 15)),
    (_d(9, 16), _d(9, 30)),
    (_d(10, 1), _d(10, 15)),
    (_d(10, 16), _d(10, 31)),
)
_GAZETTED_VALUES: dict[str, tuple[str, ...]] = {
    USD: ("87.30", "87.45", "87.60", "87.80", "88.05", "88.30", "88.55", "88.70"),
    EUR: ("101.60", "101.95", "102.25", "102.55", "102.95", "103.30", "103.75", "104.00"),
    JPY: ("0.5905", "0.5930", "0.5950", "0.5970", "0.5990", "0.6010", "0.6030", "0.6045"),
}

GAZETTED_ORIGINALS: tuple[GazettedSeed, ...] = tuple(
    GazettedSeed(pair, Decimal(value), start, end)
    for pair, values in _GAZETTED_VALUES.items()
    for (start, end), value in zip(FORTNIGHTS, values, strict=True)
)
GAZETTED_REVISIONS: tuple[GazettedSeed, ...] = (
    GazettedSeed(EUR, Decimal("103.05"), _d(9, 1), _d(9, 15), revises_period_start=_d(9, 1)),
    GazettedSeed(USD, Decimal("88.40"), _d(9, 21), _d(9, 30), revises_period_start=_d(9, 16)),
)
# Originals first: a revision's parent must exist before it is published.
SEED_GAZETTED_RATES: tuple[GazettedSeed, ...] = GAZETTED_ORIGINALS + GAZETTED_REVISIONS
