"""demurrage_rates_mcp — port demurrage tariffs and stay pricing (architecture §5).

A thin adapter over ``services.demurrage_service``. Run: ``python -m
mcp_servers.demurrage_rates_mcp.server`` (stdio).
"""

from datetime import date

from mcp_servers.common import ToolContext, configure_logging, create_server, run_tool
from models.enums import ContainerType
from models.schemas import DemurrageCharge, PortRateRecord, PortRateUpsert
from services import demurrage_service as svc

mcp = create_server(
    "demurrage_rates_mcp",
    "Port demurrage tariffs: free days and daily rate per port and container type, as dated "
    "periods. Lookups are historical — the tariff in force on the given date — and never fall "
    "back to another date. Amounts are in the tariff's own currency.",
)


@mcp.tool()
async def get_rate(
    ctx: ToolContext, port_code: str, container_type: ContainerType, as_of: date
) -> PortRateRecord:
    """The tariff in force at a port for a container type on a date (UN/LOCODE, e.g. INNSA)."""
    return await run_tool(
        ctx, "get_rate", {"port_code": port_code, "container_type": container_type, "as_of": as_of},
        lambda s, _: svc.get_rate(s, port_code, container_type, as_of),
    )  # fmt: skip


@mcp.tool()
async def get_demurrage_charge(
    ctx: ToolContext, port_code: str, container_type: ContainerType, as_of: date, days_in_port: int
) -> DemurrageCharge:
    """Price a stay: days beyond the free allowance x the daily rate in force on ``as_of``."""
    arguments = {"port_code": port_code, "container_type": container_type, "as_of": as_of,
                 "days_in_port": days_in_port}  # fmt: skip
    return await run_tool(
        ctx, "get_demurrage_charge", arguments,
        lambda s, _: svc.charge_for_stay(s, port_code, container_type, as_of, days_in_port),
    )  # fmt: skip


@mcp.tool()
async def list_rates(ctx: ToolContext, port_code: str | None = None) -> list[PortRateRecord]:
    """Every tariff period, optionally for one port, oldest first per port and container type."""
    return await run_tool(ctx, "list_rates", {"port_code": port_code},
                          lambda s, _: svc.list_rates(s, port_code))  # fmt: skip


@mcp.tool()
async def list_ports(ctx: ToolContext) -> list[str]:
    """Ports that have at least one tariff configured."""
    return await run_tool(ctx, "list_ports", {}, lambda s, _: svc.list_ports(s))


@mcp.tool()
async def update_rate(ctx: ToolContext, rate: PortRateUpsert) -> PortRateRecord:
    """Add a tariff period, or correct the one starting on the same day. A new period closes
    the one in force the day before; history is never overwritten."""
    return await run_tool(ctx, "update_rate", {"rate": rate},
                          lambda s, c: svc.upsert_rate(s, rate, c.actor))  # fmt: skip


def main() -> None:
    configure_logging()
    mcp.run()


if __name__ == "__main__":
    main()
