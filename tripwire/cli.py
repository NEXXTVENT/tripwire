"""Human-side CLI: approvals, kill-switch reset, audit verification, status."""

from __future__ import annotations

import argparse
import getpass
import json
import sys

from .config import build_firewall, load_config


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tripwire", description="Risk firewall for AI trading agents")
    ap.add_argument("--config", help="path to tripwire.yaml (default: $TRIPWIRE_CONFIG or ./tripwire.yaml)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve", help="run the MCP server (stdio) for your agent")
    sub.add_parser("status", help="account, exposure, halt state")
    sub.add_parser("policy", help="show enforced limits")
    sub.add_parser("pending", help="list pending approvals")
    a = sub.add_parser("approve", help="approve a pending order"); a.add_argument("id")
    d = sub.add_parser("deny", help="deny a pending order"); d.add_argument("id")
    sub.add_parser("reset-halt", help="clear the kill switch (re-baselines equity)")
    v = sub.add_parser("audit", help="show or verify the audit log")
    v.add_argument("action", choices=["show", "verify"])
    v.add_argument("-n", type=int, default=20)
    sub.add_parser("telegram", help="run the Telegram approval bot (long polling)")
    for name, hlp in (("check", "dry-run an order through the policy"), ("order", "place an order through the firewall")):
        o = sub.add_parser(name, help=hlp)
        o.add_argument("side", choices=["buy", "sell"])
        o.add_argument("coin")
        o.add_argument("size", type=float, help="base units, e.g. 0.01 for ETH")
        o.add_argument("--limit", type=float, help="limit price (default: market)")
        o.add_argument("--reduce-only", action="store_true")
        o.add_argument("--reason", default="manual test from CLI")
    c = sub.add_parser("close", help="close a whole position (reduce-only)")
    c.add_argument("coin")
    ex = sub.add_parser("execute", help="execute an approved order"); ex.add_argument("id")
    args = ap.parse_args(argv)

    if args.cmd == "serve":
        import os
        if args.config:
            os.environ["TRIPWIRE_CONFIG"] = args.config
        from .server import main as serve
        serve()
        return 0

    fw = build_firewall(load_config(args.config))
    who = f"cli:{getpass.getuser()}"

    if args.cmd == "status":
        _print(fw.status())
    elif args.cmd == "policy":
        _print(fw.engine.policy.to_dict())
    elif args.cmd == "pending":
        _print(fw.approvals.list("pending"))
    elif args.cmd in ("approve", "deny"):
        try:
            rec = fw.approvals.decide(args.id, approve=(args.cmd == "approve"), by=who)
        except (KeyError, ValueError) as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        fw.audit.append(f"approval_{rec['status']}", {"approval_id": args.id, "by": who})
        _print(rec)
    elif args.cmd == "reset-halt":
        _print(fw.reset_halt(by=who))
    elif args.cmd == "audit":
        if args.action == "verify":
            ok, msg = fw.audit.verify()
            print(msg)
            return 0 if ok else 2
        _print(fw.audit.entries(last=args.n))
    elif args.cmd in ("check", "order"):
        from .models import OrderIntent
        intent = OrderIntent(args.coin.upper(), args.side == "buy", args.size,
                             "limit" if args.limit else "market", args.limit, args.reduce_only)
        _print(fw.check(intent) if args.cmd == "check" else fw.submit(intent, reason=f"[{who}] {args.reason}"))
    elif args.cmd == "close":
        _print(fw.close_position(args.coin, reason=f"[{who}] manual close"))
    elif args.cmd == "execute":
        _print(fw.execute_approved(args.id))
    elif args.cmd == "telegram":
        from .notify import TelegramNotifier
        if not isinstance(fw.notifier, TelegramNotifier):
            print("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID first.", file=sys.stderr)
            return 1
        print("Telegram approval bot running. Ctrl+C to stop.")
        fw.notifier.poll_forever(fw.approvals, fw.audit)
    return 0


if __name__ == "__main__":
    sys.exit(main())
