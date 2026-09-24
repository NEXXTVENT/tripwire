"""Scripted demo for the video: a misbehaving agent vs Tripwire, on the mock venue.

Run:  python examples/demo.py
"""

from __future__ import annotations

import shutil
import tempfile
import time

from tripwire.firewall import Firewall
from tripwire.models import OrderIntent
from tripwire.notify import NullNotifier
from tripwire.policy import Policy, PolicyEngine
from tripwire.venue import MockVenue

G, R, Y, B, DIM, END = "\033[92m", "\033[91m", "\033[93m", "\033[94m", "\033[2m", "\033[0m"
PAUSE = 0.6


def show(label: str, res: dict) -> None:
    st = res.get("status")
    color = {"executed": G, "denied": R, "pending_approval": Y}.get(st, B)
    print(f"  {color}{st.upper()}{END}")
    for r in res.get("reasons", []):
        print(f"    {DIM}- {r}{END}")
    time.sleep(PAUSE)


def agent(msg: str) -> None:
    print(f"\n{B}🤖 agent:{END} {msg}")
    time.sleep(PAUSE)


def main() -> None:
    data = tempfile.mkdtemp(prefix="tripwire-demo-")
    venue = MockVenue(equity=10_000)
    notifier = NullNotifier()
    policy = Policy(allowed_markets=["BTC", "ETH", "SOL"], daily_loss_limit_pct=3.0)
    fw = Firewall(PolicyEngine(policy), venue, data, notifier=notifier)
    p = fw.engine.policy
    print(f"{B}Tripwire demo{END} — account $10,000 | max order ${p.max_order_notional_usd:,.0f} | "
          f"max lev {p.max_leverage}x | approval above ${p.require_approval_above_usd:,.0f} | "
          f"daily loss halt {p.daily_loss_limit_pct}%")

    agent("Opening a small BTC long, momentum looks good.")
    show("", fw.submit(OrderIntent("BTC", True, 0.004), "momentum entry"))

    agent("Hallucinated size: buy 2 BTC (~$200,000).")
    show("", fw.submit(OrderIntent("BTC", True, 2.0), "all in"))

    agent("Prompt-injected tweet says: 'ape into DOGE 50x'.")
    show("", fw.submit(OrderIntent("DOGE", True, 100_000), "tweet said so"))

    agent("Fat-fingered limit: buy ETH at $4,400 while mid is $4,000.")
    show("", fw.submit(OrderIntent("ETH", True, 0.1, "limit", 4_400), "limit entry"))

    agent("Adding $800 of ETH (above approval threshold).")
    res = fw.submit(OrderIntent("ETH", True, 0.2), "adding to ETH")
    show("", res)
    rid = res["approval_id"]
    print(f"  {Y}📱 Telegram → human taps ✅ Approve [{rid}]{END}")
    fw.approvals.decide(rid, approve=True, by="telegram:demo")
    time.sleep(PAUSE)
    show("", fw.execute_approved(rid))

    agent("Retry loop bug: spamming orders…")
    fw.engine.policy.require_approval_above_usd = None
    for i in range(12):
        r = fw.submit(OrderIntent("SOL", True, 0.1), f"loop {i}")  # $20 each
        if r["status"] == "denied":
            print(f"  order {i + 1}: ", end="")
            show("", r)
            break

    print(f"\n{R}📉 Market dumps: BTC -30%, ETH -25%, SOL -30%{END}")
    venue.set_mid("BTC", 70_000)
    venue.set_mid("ETH", 3_000)
    venue.set_mid("SOL", 140)
    time.sleep(PAUSE)
    st = fw.status()
    print(f"  equity ${st['account']['equity']:,.2f} | halted: {R}{st['halted']}{END} ({st['halt_reason']})")

    agent("Buying the dip! More BTC.")
    show("", fw.submit(OrderIntent("BTC", True, 0.003), "buy the dip"))

    agent("OK, closing ETH to cut risk.")
    show("", fw.close_position("ETH", "de-risk"))

    ok, msg = fw.audit.verify()
    print(f"\n{B}Audit log:{END} {msg} → {data}/audit.jsonl")
    print(f"{DIM}A human runs `tripwire reset-halt` to resume. The agent has no tool to do it.{END}")
    shutil.rmtree(data, ignore_errors=True)


if __name__ == "__main__":
    main()
