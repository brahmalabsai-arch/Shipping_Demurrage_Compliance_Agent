"""Seed demurrage tariffs covering the JNPT case-study dates.

INNSA (Nhava Sheva / JNPT) has two periods per container type: the 2026 base tariff, and a
revision effective 2026-10-01 with higher daily rates and one fewer free day — so historical
lookups (a shipment docked in September keeps the September rate) are exercised by real data.
INMUN (Mundra) gives the dashboard a second port. Rates are in USD per container per day; the
risk agent converts them at the gazetted port-charges FX rate (Phase 8).
"""

from datetime import date
from decimal import Decimal

from models.enums import ContainerType
from models.schemas import PortRateUpsert

C = ContainerType
BASE_FROM = date(2026, 1, 1)
REVISION_FROM = date(2026, 10, 1)


def _rate(port: str, ctype: ContainerType, free: int, daily: str, start: date) -> PortRateUpsert:
    return PortRateUpsert(
        port_code=port,
        container_type=ctype,
        currency="USD",
        free_days=free,
        daily_rate=Decimal(daily),
        effective_from=start,
    )


# Order matters only for readability: upsert closes earlier periods whatever the order.
SEED_PORT_RATES: tuple[PortRateUpsert, ...] = (
    _rate("INNSA", C.DRY_20, 5, "45.00", BASE_FROM),
    _rate("INNSA", C.DRY_40, 5, "90.00", BASE_FROM),
    _rate("INNSA", C.HIGH_CUBE_40, 5, "100.00", BASE_FROM),
    _rate("INNSA", C.DRY_20, 4, "55.00", REVISION_FROM),
    _rate("INNSA", C.DRY_40, 4, "110.00", REVISION_FROM),
    _rate("INNSA", C.HIGH_CUBE_40, 4, "120.00", REVISION_FROM),
    _rate("INMUN", C.DRY_20, 7, "40.00", BASE_FROM),
    _rate("INMUN", C.DRY_40, 7, "80.00", BASE_FROM),
)
