"""fx_rates_mcp — exchange rates and the four-stage resolution engine (architecture §5, §8).

A thin adapter over ``services.fx_service``. Run: ``python -m mcp_servers.fx_rates_mcp.server``
(stdio).
"""

from datetime import date

from mcp_servers.common import ToolContext, configure_logging, create_server, run_tool
from models.enums import FxStage, InvoiceKind, LockType
from models.schemas import (
    GazettedRateCreate,
    GazettedRateRecord,
    LockedRateCreate,
    LockedRateRecord,
    ResolvedRate,
    SpotRateRecord,
    SpotRateUpsert,
)
from services import fx_service as svc

mcp = create_server(
    "fx_rates_mcp",
    "Exchange rates into INR ('BASE/QUOTE', e.g. USD/INR = rupees per dollar). Use resolve_rate "
    "with the shipment's lifecycle stage: quotation (spot + validity window), bol (rate locked "
    "on the on-board date), invoice (commercial: own date; freight: the BoL rate) and "
    "port_charges (gazetted period rate). A missing rate is an error, never a guess.",
)


@mcp.tool()
async def resolve_rate(
    ctx: ToolContext,
    pair: str,
    as_of: date,
    stage: FxStage,
    quote_validity_days: int | None = None,
    invoice_kind: InvoiceKind | None = None,
    bol_date: date | None = None,
) -> ResolvedRate:
    """The rate for a pair at a lifecycle stage on a date. quotation needs
    quote_validity_days (7-14) and returns ``expires`` (valid through that day); invoice needs
    invoice_kind; a freight invoice also needs bol_date. Other stage context is rejected."""
    arguments = {"pair": pair, "as_of": as_of, "stage": stage,
                 "quote_validity_days": quote_validity_days, "invoice_kind": invoice_kind,
                 "bol_date": bol_date}  # fmt: skip
    return await run_tool(
        ctx, "resolve_rate", arguments,
        lambda s, _: svc.resolve_rate(s, pair, as_of, stage, quote_validity_days=quote_validity_days,
                                      invoice_kind=invoice_kind, bol_date=bol_date),
    )  # fmt: skip


@mcp.tool()
async def get_spot_rate(ctx: ToolContext, pair: str, as_of: date) -> SpotRateRecord:
    """The latest spot observation on or before the (IST) calendar day ``as_of``."""
    return await run_tool(ctx, "get_spot_rate", {"pair": pair, "as_of": as_of},
                          lambda s, _: svc.get_spot_rate(s, pair, as_of))  # fmt: skip


@mcp.tool()
async def get_locked_rate(
    ctx: ToolContext, pair: str, lock_type: LockType, lock_date: date
) -> LockedRateRecord:
    """The rate locked for a pair on exactly that BoL or commercial-invoice date."""
    return await run_tool(
        ctx, "get_locked_rate", {"pair": pair, "lock_type": lock_type, "lock_date": lock_date},
        lambda s, _: svc.get_locked_rate(s, pair, lock_type, lock_date),
    )  # fmt: skip


@mcp.tool()
async def get_gazetted_rate(ctx: ToolContext, pair: str, as_of: date) -> GazettedRateRecord:
    """The gazetted rate in force on a date, with any revision applied."""
    return await run_tool(ctx, "get_gazetted_rate", {"pair": pair, "as_of": as_of},
                          lambda s, _: svc.get_gazetted_rate(s, pair, as_of))  # fmt: skip


@mcp.tool()
async def list_pairs(ctx: ToolContext) -> list[str]:
    """Currency pairs present in any rate table."""
    return await run_tool(ctx, "list_pairs", {}, lambda s, _: svc.list_pairs(s))


@mcp.tool()
async def list_spot_rates(ctx: ToolContext, pair: str | None = None) -> list[SpotRateRecord]:
    """Spot observations, optionally for one pair, newest first."""
    return await run_tool(ctx, "list_spot_rates", {"pair": pair},
                          lambda s, _: svc.list_spot_rates(s, pair))  # fmt: skip


@mcp.tool()
async def list_locked_rates(ctx: ToolContext, pair: str | None = None) -> list[LockedRateRecord]:
    """Locked BoL / commercial-invoice rates, optionally for one pair."""
    return await run_tool(ctx, "list_locked_rates", {"pair": pair},
                          lambda s, _: svc.list_locked_rates(s, pair))  # fmt: skip


@mcp.tool()
async def list_gazetted_rates(
    ctx: ToolContext, pair: str | None = None
) -> list[GazettedRateRecord]:
    """Gazetted notifications and their revisions, optionally for one pair."""
    return await run_tool(ctx, "list_gazetted_rates", {"pair": pair},
                          lambda s, _: svc.list_gazetted_rates(s, pair))  # fmt: skip


@mcp.tool()
async def update_spot_rate(ctx: ToolContext, rate: SpotRateUpsert) -> SpotRateRecord:
    """Record a spot observation, or correct the one recorded for the same pair and instant."""
    return await run_tool(ctx, "update_spot_rate", {"rate": rate},
                          lambda s, c: svc.upsert_spot_rate(s, rate, c.actor))  # fmt: skip


@mcp.tool()
async def create_locked_rate(ctx: ToolContext, rate: LockedRateCreate) -> LockedRateRecord:
    """Lock a rate on a BoL or commercial-invoice date. Locked rates can never change."""
    return await run_tool(ctx, "create_locked_rate", {"rate": rate},
                          lambda s, c: svc.lock_rate(s, rate, c.actor))  # fmt: skip


@mcp.tool()
async def create_gazetted_rate(ctx: ToolContext, rate: GazettedRateCreate) -> GazettedRateRecord:
    """Publish a gazetted notification, or (with revision_of) a correction of an existing one."""
    return await run_tool(ctx, "create_gazetted_rate", {"rate": rate},
                          lambda s, c: svc.publish_gazetted_rate(s, rate, c.actor))  # fmt: skip


def main() -> None:
    configure_logging()
    mcp.run()


if __name__ == "__main__":
    main()
