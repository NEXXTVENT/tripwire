"""Policy definition and the rule engine.

The engine is pure: given an intent, a fresh account snapshot and the risk state,
it returns a Decision. It never talks to the network or the venue.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Optional

import yaml

from .models import AccountSnapshot, Decision, OrderIntent, Verdict
from .state import RiskState


@dataclass
class Policy:
    allowed_markets: list[str] = field(default_factory=lambda: ["BTC", "ETH"])
    max_order_notional_usd: float = 1_000.0
    max_position_notional_usd: float = 2_500.0
    max_gross_exposure_usd: float = 5_000.0
    max_leverage: float = 3.0
    max_price_deviation_pct: float = 2.0
    max_orders_per_minute: int = 10
    daily_loss_limit_pct: float = 5.0
    max_drawdown_pct: float = 15.0
    require_approval_above_usd: Optional[float] = 500.0
    allow_reducing_when_halted: bool = True

    @classmethod
    def load(cls, path: str | Path) -> "Policy":
        data = yaml.safe_load(Path(path).read_text()) or {}
        known = {f.name for f in fields(cls)}
        unknown = set(data) - known
        if unknown:
            # Fail closed: a typo in a risk limit must not silently disable it.
            raise ValueError(f"Unknown policy keys: {sorted(unknown)}")
        policy = cls(**data)
        policy.allowed_markets = [m.upper() for m in policy.allowed_markets]
        policy.validate()
        return policy

    def validate(self) -> None:
        for name in (
            "max_order_notional_usd",
            "max_position_notional_usd",
            "max_gross_exposure_usd",
            "max_leverage",
            "max_price_deviation_pct",
            "daily_loss_limit_pct",
            "max_drawdown_pct",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be > 0")
        if self.max_orders_per_minute < 1:
            raise ValueError("max_orders_per_minute must be >= 1")

    def to_dict(self) -> dict:
        return {f.name: getattr(self, f.name) for f in fields(self)}


def is_risk_reducing(intent: OrderIntent, snap: AccountSnapshot) -> bool:
    """True if the order can only shrink an existing position, never flip or grow it."""
    pos = snap.position_size(intent.coin)
    if pos == 0:
        return False
    delta = intent.size if intent.is_buy else -intent.size
    new = pos + delta
    return abs(new) < abs(pos) and (new == 0 or (new > 0) == (pos > 0))


class PolicyEngine:
    def __init__(self, policy: Policy):
        self.policy = policy

    def observe(self, snap: AccountSnapshot, state: RiskState, now: float) -> None:
        """Update day-start equity and high-water mark; latch HALT if a loss limit is hit."""
        p = self.policy
        state.roll_day(snap.equity, now)
        state.high_water_mark = max(state.high_water_mark or snap.equity, snap.equity)

        if state.halted:
            return
        if state.day_start_equity and state.day_start_equity > 0:
            day_loss = (state.day_start_equity - snap.equity) / state.day_start_equity * 100
            if day_loss >= p.daily_loss_limit_pct:
                state.halt(f"daily loss {day_loss:.2f}% >= limit {p.daily_loss_limit_pct}%", now)
                return
        if state.high_water_mark and state.high_water_mark > 0:
            dd = (state.high_water_mark - snap.equity) / state.high_water_mark * 100
            if dd >= p.max_drawdown_pct:
                state.halt(f"drawdown {dd:.2f}% >= limit {p.max_drawdown_pct}%", now)

    def evaluate(
        self,
        intent: OrderIntent,
        snap: AccountSnapshot,
        state: RiskState,
        now: float,
        approved: bool = False,
    ) -> Decision:
        p = self.policy
        deny: list[str] = []
        coin = intent.coin.upper()
        mid = snap.mids.get(coin)

        if intent.size <= 0:
            return Decision(Verdict.DENY, ["size must be > 0"])
        if coin not in p.allowed_markets:
            return Decision(Verdict.DENY, [f"market {coin} not in allowed_markets {p.allowed_markets}"])
        if not mid or mid <= 0:
            return Decision(Verdict.DENY, [f"no mid price for {coin}; refusing to trade blind"])

        reducing = is_risk_reducing(intent, snap)
        if intent.reduce_only and not reducing:
            deny.append("reduce_only order would not reduce an existing position")

        # Pricing sanity (fat-finger guard).
        px = mid
        if intent.order_type == "limit":
            if intent.limit_px is None or intent.limit_px <= 0:
                return Decision(Verdict.DENY, ["limit order needs a positive limit_px"])
            px = intent.limit_px
            dev = abs(px - mid) / mid * 100
            if dev > p.max_price_deviation_pct:
                deny.append(f"limit price {px} is {dev:.2f}% from mid {mid} (max {p.max_price_deviation_pct}%)")
        elif intent.order_type != "market":
            return Decision(Verdict.DENY, [f"unknown order_type {intent.order_type!r}"])

        order_notional = intent.size * px
        pos = snap.position_size(coin)
        new_pos = pos + (intent.size if intent.is_buy else -intent.size)
        new_pos_notional = abs(new_pos) * mid
        new_gross = snap.gross_exposure() - abs(pos) * mid + new_pos_notional
        leverage = new_gross / snap.equity if snap.equity > 0 else float("inf")

        metrics = {
            "mid": mid,
            "order_notional_usd": round(order_notional, 2),
            "position_after": round(new_pos, 8),
            "position_notional_after_usd": round(new_pos_notional, 2),
            "gross_exposure_after_usd": round(new_gross, 2),
            "leverage_after": round(leverage, 3),
            "equity": snap.equity,
            "risk_reducing": reducing,
            "halted": state.halted,
        }

        # Kill switch: only de-risking gets through.
        if state.halted:
            if not (reducing and p.allow_reducing_when_halted):
                deny.append(f"HALTED ({state.halt_reason}); only risk-reducing orders allowed until a human resets")
            if deny:
                return Decision(Verdict.DENY, deny, metrics)
            return Decision(Verdict.ALLOW, ["halted, but order reduces risk"], metrics)

        if state.orders_in_last(60, now) >= p.max_orders_per_minute:
            deny.append(f"rate limit: {p.max_orders_per_minute} orders/minute")

        # Size limits only bind on orders that add risk; closing a position is always cheaper.
        if not reducing:
            if order_notional > p.max_order_notional_usd:
                deny.append(f"order notional ${order_notional:,.0f} > max ${p.max_order_notional_usd:,.0f}")
            if new_pos_notional > p.max_position_notional_usd:
                deny.append(
                    f"{coin} position after trade ${new_pos_notional:,.0f} > max ${p.max_position_notional_usd:,.0f}"
                )
            if new_gross > p.max_gross_exposure_usd:
                deny.append(f"gross exposure after trade ${new_gross:,.0f} > max ${p.max_gross_exposure_usd:,.0f}")
            if leverage > p.max_leverage:
                deny.append(f"leverage after trade {leverage:.2f}x > max {p.max_leverage}x")

        if deny:
            return Decision(Verdict.DENY, deny, metrics)

        if (
            not approved
            and not reducing
            and p.require_approval_above_usd is not None
            and order_notional > p.require_approval_above_usd
        ):
            return Decision(
                Verdict.NEEDS_APPROVAL,
                [f"order notional ${order_notional:,.0f} > approval threshold ${p.require_approval_above_usd:,.0f}"],
                metrics,
            )

        return Decision(Verdict.ALLOW, ["all checks passed"], metrics)
