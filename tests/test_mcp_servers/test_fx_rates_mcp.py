"""Phase 5 — fx_rates_mcp: every tool matches the direct service call byte for byte."""

from datetime import UTC, date, datetime
from typing import Any

import pytest

from mcp_servers.fx_rates_mcp.server import mcp
from models.enums import FxStage, InvoiceKind, LockType
from services import fx_service as svc
from tests.test_mcp_servers.conftest import assert_parity, assert_tool_error, connect, structured

TOOLS_TESTED = {
    "resolve_rate",
    "get_spot_rate",
    "get_locked_rate",
    "get_gazetted_rate",
    "list_pairs",
    "list_spot_rates",
    "list_locked_rates",
    "list_gazetted_rates",
    "update_spot_rate",
    "create_locked_rate",
    "create_gazetted_rate",
}
USD, EUR = "USD/INR", "EUR/INR"


def _d(month: int, day: int) -> date:
    return date(2026, month, day)


READS: list[tuple[str, dict[str, Any], Any]] = [
    # the four resolution stages
    ("resolve_rate",
     {"pair": USD, "as_of": "2026-08-10", "stage": "quotation", "quote_validity_days": 14},
     lambda s: svc.resolve_rate(s, USD, _d(8, 10), FxStage.QUOTATION, quote_validity_days=14)),
    ("resolve_rate", {"pair": USD, "as_of": "2026-08-22", "stage": "bol"},
     lambda s: svc.resolve_rate(s, USD, _d(8, 22), FxStage.BOL)),
    ("resolve_rate",
     {"pair": USD, "as_of": "2026-08-20", "stage": "invoice", "invoice_kind": "freight",
      "bol_date": "2026-08-25"},
     lambda s: svc.resolve_rate(s, USD, _d(8, 20), FxStage.INVOICE,
                                invoice_kind=InvoiceKind.FREIGHT, bol_date=_d(8, 25))),
    ("resolve_rate", {"pair": EUR, "as_of": "2026-09-08", "stage": "port_charges"},
     lambda s: svc.resolve_rate(s, EUR, _d(9, 8), FxStage.PORT_CHARGES)),
    ("get_spot_rate", {"pair": USD, "as_of": "2026-09-20"},
     lambda s: svc.get_spot_rate(s, USD, _d(9, 20))),
    ("get_locked_rate", {"pair": EUR, "lock_type": "commercial_invoice", "lock_date": "2026-07-28"},
     lambda s: svc.get_locked_rate(s, EUR, LockType.COMMERCIAL_INVOICE, _d(7, 28))),
    ("get_gazetted_rate", {"pair": USD, "as_of": "2026-09-22"},
     lambda s: svc.get_gazetted_rate(s, USD, _d(9, 22))),
    ("list_pairs", {}, lambda s: svc.list_pairs(s)),
    ("list_spot_rates", {"pair": USD}, lambda s: svc.list_spot_rates(s, USD)),
    ("list_locked_rates", {}, lambda s: svc.list_locked_rates(s)),
    ("list_gazetted_rates", {"pair": EUR}, lambda s: svc.list_gazetted_rates(s, EUR)),
]  # fmt: skip


@pytest.mark.parametrize(("tool", "arguments", "service_call"), READS)
async def test_read_tools_match_the_service(
    seeded: None, direct: Any, tool: str, arguments: dict[str, Any], service_call: Any
) -> None:
    async with connect(mcp) as client:
        result = await client.call_tool(tool, arguments)

    assert_parity(result, direct(service_call))


async def test_write_tools_write_through_the_service(seeded: None, direct: Any) -> None:
    async with connect(mcp) as client:
        spot = await client.call_tool(
            "update_spot_rate",
            {
                "rate": {
                    "pair": USD,
                    "rate": "88.5000",
                    "as_of": "2026-10-06T08:00:00Z",
                    "source": "desk",
                }
            },
        )
        locked = await client.call_tool(
            "create_locked_rate",
            {
                "rate": {
                    "pair": USD,
                    "rate": "88.4400",
                    "lock_date": "2026-10-06",
                    "lock_type": "bol",
                }
            },
        )
        original = structured(
            await client.call_tool("get_gazetted_rate", {"pair": USD, "as_of": "2026-10-10"})
        )
        revision = await client.call_tool(
            "create_gazetted_rate",
            {
                "rate": {
                    "pair": USD,
                    "rate": "88.60",
                    "period_start": "2026-10-08",
                    "period_end": "2026-10-15",
                    "publishing_authority": "CBIC (simulated)",
                    "revision_of": original["id"],
                }
            },
        )

    assert_parity(spot, direct(lambda s: svc.get_spot_rate(s, USD, _d(10, 6))))
    assert_parity(locked, direct(lambda s: svc.get_locked_rate(s, USD, LockType.BOL, _d(10, 6))))
    assert_parity(revision, direct(lambda s: svc.get_gazetted_rate(s, USD, _d(10, 10))))
    assert spot is not None and direct(
        lambda s: svc.get_spot_rate(s, USD, _d(10, 6))
    ).as_of == datetime(2026, 10, 6, 8, tzinfo=UTC)


async def test_rules_of_the_engine_surface_as_typed_errors(seeded: None) -> None:
    async with connect(mcp) as client:
        saturday = await client.call_tool(
            "resolve_rate", {"pair": USD, "as_of": "2026-08-29", "stage": "bol"}
        )
        no_validity = await client.call_tool(
            "resolve_rate", {"pair": USD, "as_of": "2026-08-10", "stage": "quotation"}
        )
        relock = await client.call_tool("create_locked_rate", {"rate": {
            "pair": USD, "rate": "90", "lock_date": "2026-08-22", "lock_type": "bol"}})  # fmt: skip

    assert_tool_error(saturday, "RateNotFoundError")
    assert_tool_error(no_validity, "InvalidRateRequestError")
    assert_tool_error(relock, "RateAlreadyLockedError")
