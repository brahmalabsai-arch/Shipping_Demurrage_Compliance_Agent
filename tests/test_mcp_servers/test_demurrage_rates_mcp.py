"""Phase 5 — demurrage_rates_mcp: every tool matches the direct service call byte for byte."""

from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from mcp_servers.demurrage_rates_mcp.server import mcp
from models.enums import ContainerType
from services import demurrage_service as svc
from tests.test_mcp_servers.conftest import assert_parity, assert_tool_error, connect, structured

TOOLS_TESTED = {"get_rate", "get_demurrage_charge", "list_rates", "list_ports", "update_rate"}
C40 = ContainerType.DRY_40
DOCKED = date(2026, 9, 8)

READS: list[tuple[str, dict[str, Any], Any]] = [
    ("get_rate", {"port_code": "INNSA", "container_type": "40ft", "as_of": "2026-09-08"},
     lambda s: svc.get_rate(s, "INNSA", C40, DOCKED)),
    ("get_demurrage_charge",
     {"port_code": "INNSA", "container_type": "40ft", "as_of": "2026-09-08", "days_in_port": 12},
     lambda s: svc.demurrage_charge(svc.get_rate(s, "INNSA", C40, DOCKED), 12)),
    ("list_rates", {}, lambda s: svc.list_rates(s)),
    ("list_rates", {"port_code": "INMUN"}, lambda s: svc.list_rates(s, "INMUN")),
    ("list_ports", {}, lambda s: svc.list_ports(s)),
]  # fmt: skip


@pytest.mark.parametrize(("tool", "arguments", "service_call"), READS)
async def test_read_tools_match_the_service(
    seeded: None, direct: Any, tool: str, arguments: dict[str, Any], service_call: Any
) -> None:
    async with connect(mcp) as client:
        result = await client.call_tool(tool, arguments)

    assert_parity(result, direct(service_call))


async def test_charge_for_the_delayed_fixture(seeded: None) -> None:
    async with connect(mcp) as client:
        charge = structured(await client.call_tool(
            "get_demurrage_charge",
            {"port_code": "INNSA", "container_type": "40ft", "as_of": "2026-09-08",
             "days_in_port": 12},
        ))  # fmt: skip

    # 5 free days, 90.00 USD/day in September: (12 - 5) x 90 = 630
    assert (charge["chargeable_days"], charge["amount"], charge["currency"]) == (7, "630.00", "USD")


async def test_update_rate_writes_through_the_service(seeded: None, direct: Any) -> None:
    revision = {"port_code": "INNSA", "container_type": "40ft", "currency": "USD",
                "free_days": 3, "daily_rate": "130.00", "effective_from": "2026-12-01"}  # fmt: skip
    async with connect(mcp) as client:
        result = await client.call_tool("update_rate", {"rate": revision})

    assert_parity(result, direct(lambda s: svc.get_rate(s, "INNSA", C40, date(2026, 12, 5))))
    assert direct(lambda s: svc.get_rate(s, "INNSA", C40, date(2026, 11, 30))).effective_to == date(
        2026, 11, 30
    )


async def test_errors_are_typed_and_validation_happens_first(seeded: None, direct: Any) -> None:
    async with connect(mcp) as client:
        unknown = await client.call_tool(
            "get_rate", {"port_code": "XXABC", "container_type": "20ft", "as_of": "2026-09-08"}
        )
        bad_type = await client.call_tool(
            "get_rate", {"port_code": "INNSA", "container_type": "30ft", "as_of": "2026-09-08"}
        )
        negative = await client.call_tool(
            "update_rate",
            {"rate": {"port_code": "INNSA", "container_type": "20ft", "currency": "USD",
                      "free_days": 5, "daily_rate": "-1", "effective_from": "2026-12-01"}},
        )  # fmt: skip

    assert_tool_error(unknown, "PortRateNotFoundError")
    assert bad_type.isError and "container_type" in bad_type.content[0].text  # type: ignore[union-attr]
    assert negative.isError and "daily_rate" in negative.content[0].text  # type: ignore[union-attr]
    rates: list[Any] = direct(lambda s: svc.list_rates(s, "INNSA"))
    assert Decimal("-1") not in {r.daily_rate for r in rates}
