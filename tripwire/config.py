"""Build a Firewall from a config file + environment. Secrets come only from env vars."""

from __future__ import annotations

import os
from pathlib import Path

import yaml

from .firewall import Firewall
from .notify import NullNotifier, TelegramNotifier
from .policy import Policy, PolicyEngine
from .venue import HyperliquidVenue, MockVenue

DEFAULT_CONFIG = "tripwire.yaml"


def load_config(path: str | None = None) -> dict:
    path = path or os.environ.get("TRIPWIRE_CONFIG", DEFAULT_CONFIG)
    p = Path(path)
    cfg = yaml.safe_load(p.read_text()) if p.exists() else {}
    cfg["_base"] = str(p.resolve().parent)
    return cfg


def _resolve(base: str, rel: str) -> Path:
    p = Path(rel).expanduser()
    return p if p.is_absolute() else Path(base) / p


def build_firewall(cfg: dict) -> Firewall:
    base = cfg["_base"]
    policy_path = _resolve(base, cfg.get("policy", "policy.yaml"))
    policy = Policy.load(policy_path) if policy_path.exists() else Policy()
    data_dir = _resolve(base, cfg.get("data_dir", ".tripwire"))

    venue_kind = cfg.get("venue", "mock")
    if venue_kind == "mock":
        venue = MockVenue(equity=float(cfg.get("mock_equity", 10_000)))
    elif venue_kind == "hyperliquid":
        network = cfg.get("network", "testnet")
        if network == "mainnet" and os.environ.get("TRIPWIRE_ALLOW_MAINNET") != "1":
            raise SystemExit("Refusing mainnet: set TRIPWIRE_ALLOW_MAINNET=1 once you have tested on testnet.")
        key = os.environ.get("HL_AGENT_PRIVATE_KEY")
        account = cfg.get("account_address") or os.environ.get("HL_ACCOUNT_ADDRESS")
        if not key or not account:
            raise SystemExit("Set HL_AGENT_PRIVATE_KEY (agent wallet) and account_address / HL_ACCOUNT_ADDRESS.")
        venue = HyperliquidVenue(key, account, testnet=(network != "mainnet"),
                                 slippage=float(cfg.get("market_slippage", 0.01)))
    else:
        raise SystemExit(f"unknown venue {venue_kind!r}")

    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    notifier = TelegramNotifier(token, chat) if token and chat else NullNotifier()

    return Firewall(PolicyEngine(policy), venue, data_dir, notifier=notifier,
                    approval_ttl=int(cfg.get("approval_ttl_seconds", 600)))
