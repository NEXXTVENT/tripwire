"""Plain data types shared across the firewall."""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Optional


class Verdict(str, Enum):
    ALLOW = "ALLOW"
    DENY = "DENY"
    NEEDS_APPROVAL = "NEEDS_APPROVAL"


@dataclass
class OrderIntent:
    """What the agent wants to do. Size is in base units (e.g. BTC), always positive."""

    coin: str
    is_buy: bool
    size: float
    order_type: str = "market"  # "market" | "limit"
    limit_px: Optional[float] = None
    reduce_only: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Position:
    coin: str
    szi: float  # signed size: + long, - short
    entry_px: float = 0.0


@dataclass
class AccountSnapshot:
    equity: float
    positions: dict[str, Position]
    mids: dict[str, float]

    def position_size(self, coin: str) -> float:
        p = self.positions.get(coin)
        return p.szi if p else 0.0

    def gross_exposure(self) -> float:
        return sum(abs(p.szi) * self.mids.get(c, p.entry_px) for c, p in self.positions.items())

    def to_dict(self) -> dict:
        return {
            "equity": self.equity,
            "positions": {c: asdict(p) for c, p in self.positions.items()},
            "gross_exposure": round(self.gross_exposure(), 2),
        }


@dataclass
class Decision:
    verdict: Verdict
    reasons: list[str] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"verdict": self.verdict.value, "reasons": self.reasons, "metrics": self.metrics}
