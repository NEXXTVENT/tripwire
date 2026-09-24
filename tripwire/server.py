"""MCP server exposing ONLY guarded tools to the agent.

Deliberately absent: any tool to edit the policy, reset the kill switch, approve
orders, transfer or withdraw. Those are human-only (CLI / Telegram).
"""

from __future__ import annotations

from typing import Literal, Optional

from mcp.server.fastmcp import FastMCP

from .config import build_firewall, load_config
from .models import OrderIntent

mcp = FastMCP(
    "tripwire",
    instructions=(
        "All Hyperliquid trading goes through Tripwire, a risk firewall. Always call check_order "
        "before place_order. Every order needs a short `reason`; it is written to an audit log a "
        "human reviews. If an order is denied, do not retry with tweaked sizes to get around the "
        "limit — report the denial to the user. You cannot change limits or reset a halt."
    ),
)
_fw = None


def fw():
    global _fw
    if _fw is None:
        _fw = build_firewall(load_config())
    return _fw


def _intent(coin, side, size, order_type, limit_px, reduce_only) -> OrderIntent:
    return OrderIntent(coin=coin, is_buy=(side == "buy"), size=size, order_type=order_type,
                       limit_px=limit_px, reduce_only=reduce_only)


@mcp.tool()
def get_status() -> dict:
    """Account equity, positions, exposure, kill-switch state and pending approvals."""
    return fw().status()


@mcp.tool()
def get_policy() -> dict:
    """The risk limits currently enforced. Read-only."""
    return fw().engine.policy.to_dict()


@mcp.tool()
def check_order(coin: str, side: Literal["buy", "sell"], size: float,
                order_type: Literal["market", "limit"] = "market",
                limit_px: Optional[float] = None, reduce_only: bool = False) -> dict:
    """Dry-run an order against the policy. Nothing is sent. `size` is in base units (e.g. 0.01 BTC)."""
    return fw().check(_intent(coin, side, size, order_type, limit_px, reduce_only))


@mcp.tool()
def place_order(coin: str, side: Literal["buy", "sell"], size: float, reason: str,
                order_type: Literal["market", "limit"] = "market",
                limit_px: Optional[float] = None, reduce_only: bool = False) -> dict:
    """Submit an order through the firewall. Returns executed / denied / pending_approval."""
    return fw().submit(_intent(coin, side, size, order_type, limit_px, reduce_only), reason=reason)


@mcp.tool()
def close_position(coin: str, reason: str) -> dict:
    """Close the whole position in `coin` with a reduce-only market order (allowed even when halted)."""
    return fw().close_position(coin, reason=reason)


@mcp.tool()
def cancel_order(coin: str, oid: int, reason: str) -> dict:
    """Cancel a resting order by order id."""
    return fw().cancel(coin, oid, reason=reason)


@mcp.tool()
def get_approval(approval_id: str) -> dict:
    """Status of a pending human approval: pending / approved / denied / expired / executed."""
    rec = fw().approvals.get(approval_id)
    return rec or {"error": f"no approval {approval_id}"}


@mcp.tool()
def execute_approved(approval_id: str) -> dict:
    """Execute an order a human approved. Re-checked against the policy at execution time."""
    return fw().execute_approved(approval_id)


@mcp.tool()
def get_audit_log(last: int = 20) -> list[dict]:
    """Most recent audit entries (read-only)."""
    return fw().audit.entries(last=min(last, 200))


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
