# Tripwire — spec (v0, hackathon build)

*Working name. Rename freely before submission.*

## Problem

AI agents now trade on Hyperliquid through MCP servers, OpenClaw skills and CLIs.
Hyperliquid's agent (API) wallets already stop an agent from **withdrawing**, but
nothing stops it from **trading the account into the ground**: a hallucinated size,
a prompt-injected instruction, a retry loop, a 50x position on an illiquid market.

Existing tools offer confirm prompts and dry-runs. None enforce **portfolio-level
risk rules the agent cannot see around or switch off**.

## Product

A risk firewall that sits between any AI agent and Hyperliquid.

```
 agent (Claude / OpenClaw / any MCP client)
        │  MCP tools: check_order, place_order, close_position, ...
        ▼
 ┌──────────────── Tripwire ────────────────┐
 │  1. snapshot account (equity, positions)  │
 │  2. policy engine → ALLOW / DENY / ASK    │
 │  3. hash-chained audit log (every call)   │
 │  4. ASK → human approval (CLI / Telegram) │
 └───────────────────┬──────────────────────┘
                     ▼
     Hyperliquid (agent wallet: can trade, cannot withdraw)
```

The agent only ever holds the MCP connection. The signing key lives in Tripwire.
The policy file, kill-switch reset and approvals are **human-only** (CLI / Telegram);
no MCP tool can change them.

## Policy rules (v0)

| Rule | Effect |
|---|---|
| `allowed_markets` | DENY any market not on the list |
| `max_order_notional_usd` | DENY single orders above this |
| `max_position_notional_usd` | DENY if post-trade position in one market exceeds |
| `max_gross_exposure_usd` | DENY if post-trade total exposure exceeds |
| `max_leverage` | DENY if post-trade gross exposure / equity exceeds |
| `max_price_deviation_pct` | DENY limit orders too far from mid (fat-finger) |
| `max_orders_per_minute` | DENY above rate (runaway loops) |
| `daily_loss_limit_pct` | HALT: equity down X% from UTC-day start → only risk-reducing orders |
| `max_drawdown_pct` | HALT: equity down X% from high-water mark |
| `require_approval_above_usd` | ASK a human before orders above this |

HALT latches until a human runs `tripwire reset-halt`. While halted, orders that
reduce exposure are still allowed so the agent can de-risk.

## Guarantees and non-guarantees

- Enforced at order time against a fresh account snapshot. Approved orders are
  re-checked at execution time (prices move while you think).
- Does **not** protect against a human who runs orders outside Tripwire with the
  master key. Use a dedicated agent wallet; keep the master key offline.
- Daily-loss math treats deposits/withdrawals as P&L (v0 limitation).
- Positions opened outside Tripwire still count toward limits (snapshot is the truth).

## Roadmap (post-hackathon)

- On-chain enforcement via HyperEVM (policy as contract, not process)
- Hosted version for vault leaders running agent strategies
- Per-agent budgets when several agents share one account
- Policy templates (conservative / scalper / vault-safe)
