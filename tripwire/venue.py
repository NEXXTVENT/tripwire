"""Venue adapters. The firewall only talks to venues through this interface."""

from __future__ import annotations

from typing import Any, Optional, Protocol

from .models import AccountSnapshot, OrderIntent, Position


class Venue(Protocol):
    def snapshot(self) -> AccountSnapshot: ...
    def normalize(self, intent: OrderIntent) -> OrderIntent: ...
    def place(self, intent: OrderIntent) -> dict[str, Any]: ...
    def cancel(self, coin: str, oid: int) -> dict[str, Any]: ...


class MockVenue:
    """In-memory exchange for tests and demos. Market orders fill at mid; equity marks to market."""

    def __init__(self, equity: float = 10_000.0, mids: Optional[dict[str, float]] = None):
        self.cash = equity
        self.mids = dict(mids or {"BTC": 100_000.0, "ETH": 4_000.0, "SOL": 200.0, "HYPE": 40.0})
        self.positions: dict[str, Position] = {}
        self.resting: dict[int, dict] = {}
        self._oid = 1000
        self.fills: list[dict] = []

    # --- helpers for demos/tests ---
    def set_mid(self, coin: str, px: float) -> None:
        self.mids[coin] = px

    def _equity(self) -> float:
        upnl = sum(p.szi * (self.mids[c] - p.entry_px) for c, p in self.positions.items())
        return self.cash + upnl

    # --- Venue interface ---
    def snapshot(self) -> AccountSnapshot:
        return AccountSnapshot(
            equity=round(self._equity(), 2),
            positions={c: Position(c, p.szi, p.entry_px) for c, p in self.positions.items()},
            mids=dict(self.mids),
        )

    def normalize(self, intent: OrderIntent) -> OrderIntent:
        intent.coin = intent.coin.upper()
        intent.size = round(intent.size, 6)
        return intent

    def place(self, intent: OrderIntent) -> dict[str, Any]:
        if intent.order_type == "limit":
            self._oid += 1
            self.resting[self._oid] = intent.to_dict()
            return {"status": "resting", "oid": self._oid}
        px = self.mids[intent.coin]
        delta = intent.size if intent.is_buy else -intent.size
        pos = self.positions.get(intent.coin)
        old = pos.szi if pos else 0.0
        new = old + delta
        # realize P&L on the closed part
        if old != 0 and (old > 0) != (delta > 0):
            closed = min(abs(delta), abs(old))
            self.cash += closed * (px - pos.entry_px) * (1 if old > 0 else -1)
        if abs(new) < 1e-12:
            self.positions.pop(intent.coin, None)
        elif old == 0 or (old > 0) != (new > 0):
            self.positions[intent.coin] = Position(intent.coin, new, px)
        elif abs(new) > abs(old):
            entry = (abs(old) * pos.entry_px + abs(delta) * px) / abs(new)
            self.positions[intent.coin] = Position(intent.coin, new, entry)
        else:
            self.positions[intent.coin] = Position(intent.coin, new, pos.entry_px)
        fill = {"status": "filled", "coin": intent.coin, "size": intent.size, "px": px, "is_buy": intent.is_buy}
        self.fills.append(fill)
        return fill

    def cancel(self, coin: str, oid: int) -> dict[str, Any]:
        if self.resting.pop(oid, None) is None:
            return {"status": "error", "error": f"no resting order {oid}"}
        return {"status": "cancelled", "oid": oid}


class HyperliquidVenue:
    """Live adapter using the official hyperliquid-python-sdk.

    `agent_key` should be an API (agent) wallet key: it can trade but cannot withdraw.
    `account_address` is the master account the agent trades for (positions are read from it).
    """

    def __init__(self, agent_key: str, account_address: str, testnet: bool = True, slippage: float = 0.01):
        import eth_account
        from hyperliquid.exchange import Exchange
        from hyperliquid.info import Info
        from hyperliquid.utils import constants

        url = constants.TESTNET_API_URL if testnet else constants.MAINNET_API_URL
        self.account_address = account_address
        self.slippage = slippage
        self.info = Info(url, skip_ws=True)
        wallet = eth_account.Account.from_key(agent_key)
        self.exchange = Exchange(wallet, url, account_address=account_address)
        meta = self.info.meta()
        self.sz_decimals = {a["name"]: a["szDecimals"] for a in meta["universe"]}

    def snapshot(self) -> AccountSnapshot:
        st = self.info.user_state(self.account_address)
        mids = {k: float(v) for k, v in self.info.all_mids().items()}
        positions = {}
        for ap in st.get("assetPositions", []):
            p = ap["position"]
            szi = float(p["szi"])
            if szi != 0:
                positions[p["coin"]] = Position(p["coin"], szi, float(p.get("entryPx") or 0))
        equity = float(st["marginSummary"]["accountValue"])
        return AccountSnapshot(equity=equity, positions=positions, mids=mids)

    def normalize(self, intent: OrderIntent) -> OrderIntent:
        intent.coin = intent.coin.upper()
        dec = self.sz_decimals.get(intent.coin)
        if dec is not None:
            intent.size = round(intent.size, dec)
            if intent.limit_px:
                # Hyperliquid perps: 5 significant figures, max (6 - szDecimals) decimals.
                intent.limit_px = round(float(f"{intent.limit_px:.5g}"), 6 - dec)
        return intent

    def place(self, intent: OrderIntent) -> dict[str, Any]:
        if intent.order_type == "market":
            px = self.exchange._slippage_price(intent.coin, intent.is_buy, self.slippage)
            order_type = {"limit": {"tif": "Ioc"}}
        else:
            px = intent.limit_px
            order_type = {"limit": {"tif": "Gtc"}}
        resp = self.exchange.order(intent.coin, intent.is_buy, intent.size, px, order_type, intent.reduce_only)
        return self._parse(resp)

    def cancel(self, coin: str, oid: int) -> dict[str, Any]:
        return self._parse(self.exchange.cancel(coin.upper(), oid))

    @staticmethod
    def _parse(resp: Any) -> dict[str, Any]:
        if not isinstance(resp, dict) or resp.get("status") != "ok":
            return {"status": "error", "error": str(resp)}
        statuses = resp.get("response", {}).get("data", {}).get("statuses", [])
        s = statuses[0] if statuses else {}
        if isinstance(s, dict) and "error" in s:
            return {"status": "error", "error": s["error"], "raw": resp}
        if isinstance(s, dict) and "filled" in s:
            f = s["filled"]
            return {"status": "filled", "oid": f.get("oid"), "size": f.get("totalSz"), "px": f.get("avgPx"), "raw": resp}
        if isinstance(s, dict) and "resting" in s:
            return {"status": "resting", "oid": s["resting"].get("oid"), "raw": resp}
        return {"status": "ok", "raw": resp}
