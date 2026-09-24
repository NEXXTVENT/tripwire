"""Persistent risk state: day-start equity, high-water mark, kill switch, order rate."""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


def utc_day(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


@dataclass
class RiskState:
    day: Optional[str] = None
    day_start_equity: Optional[float] = None
    high_water_mark: Optional[float] = None
    halted: bool = False
    halt_reason: Optional[str] = None
    halted_at: Optional[float] = None
    order_times: list[float] = field(default_factory=list)

    def roll_day(self, equity: float, now: float) -> None:
        today = utc_day(now)
        # Re-baseline on a new day, or if the baseline is unusable (e.g. account was empty
        # when first seen and got funded later); otherwise the daily-loss limit never fires.
        if self.day != today or not self.day_start_equity or self.day_start_equity <= 0:
            self.day = today
            self.day_start_equity = equity

    def halt(self, reason: str, now: float) -> None:
        self.halted = True
        self.halt_reason = reason
        self.halted_at = now

    def reset_halt(self, equity: Optional[float] = None) -> None:
        """Human-only. Clears the kill switch and re-baselines so it doesn't re-fire instantly."""
        self.halted = False
        self.halt_reason = None
        self.halted_at = None
        if equity is not None:
            self.day_start_equity = equity
            self.high_water_mark = equity

    def record_order(self, now: float) -> None:
        self.order_times.append(now)
        self.order_times = [t for t in self.order_times if now - t < 3600]

    def orders_in_last(self, seconds: float, now: float) -> int:
        return sum(1 for t in self.order_times if now - t < seconds)


class StateStore:
    """JSON file with atomic writes so a crash can't leave a half-written kill switch."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def load(self) -> RiskState:
        if not self.path.exists():
            return RiskState()
        return RiskState(**json.loads(self.path.read_text()))

    def save(self, state: RiskState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(f".tmp.{os.getpid()}.{int(time.time() * 1e6)}")
        tmp.write_text(json.dumps(asdict(state), indent=2))
        os.replace(tmp, self.path)
