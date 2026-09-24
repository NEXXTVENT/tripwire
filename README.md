# Tripwire

**A risk firewall between AI trading agents and Hyperliquid.**

Hyperliquid agent wallets stop an AI from *withdrawing*. Nothing stops it from
*trading your account into the ground*: a hallucinated size, a prompt-injected
"ape into X", a retry loop, a fat-fingered limit. Tripwire is an MCP server that
sits between the agent and the exchange and enforces risk rules the agent can't
see around or switch off.

```
agent (Claude / OpenClaw / any MCP client)
   │  check_order · place_order · close_position · ...
   ▼
Tripwire ── snapshot → policy → hash-chained audit → human approval
   │
   ▼
Hyperliquid (agent wallet: can trade, cannot withdraw)
```

## What it enforces

| Rule | Effect |
|---|---|
| Allowed markets | anything else is denied |
| Max order / position / gross exposure (USD) | denied if the trade would exceed |
| Max leverage (after the trade) | denied if exceeded |
| Fat-finger guard | limit price too far from mid is denied |
| Rate limit | stops runaway loops |
| Daily loss limit, max drawdown | **kill switch**: latches; only de-risking allowed until a human resets |
| Approval threshold | orders above $X wait for ✅ in Telegram or the CLI |

Plus:
- Approved orders are **re-checked at execution** (prices move while you decide), and each approval can be spent once.
- The agent gets **no tool** to edit limits, reset the kill switch, approve, transfer or withdraw.
- Every request, decision, approval and fill goes into a **hash-chained audit log**, so any edit or deleted line is detected (`tripwire audit verify`).
- Fails closed: unknown policy keys, missing prices and unknown order types are all rejected.

## Try it in 30 seconds (no keys, mock exchange)

```bash
pip install -e ".[dev]"
python examples/demo.py      # scripted misbehaving agent vs Tripwire
pytest -q                    # 42 tests
```

## Connect an agent

Claude Desktop / Claude Code / any MCP client:

```json
{
  "mcpServers": {
    "tripwire": {
      "command": "tripwire",
      "args": ["--config", "/path/to/tripwire.yaml", "serve"],
      "env": { "HL_AGENT_PRIVATE_KEY": "0x...agent-wallet-key..." }
    }
  }
}
```

Copy `examples/tripwire.yaml` and `examples/policy.yaml` next to each other and edit.

## Testnet runbook

1. **Fresh wallet.** Create a new wallet for testing only. Never use a wallet that holds real funds or runs another bot.
2. **Testnet funds.** Get testnet USDC from the Hyperliquid testnet faucet (app.hyperliquid-testnet.xyz; the faucet may require a prior mainnet deposit from that address, check the current rules).
3. **Agent wallet.** On the testnet app: More → API → generate an API (agent) wallet and authorize it. Copy its private key. This key can trade, **not withdraw**.
4. **Config.** In `tripwire.yaml` set `venue: hyperliquid`, `network: testnet`, `account_address: <your master address>` (the funded one, not the agent address).
5. **Env.** `export HL_AGENT_PRIVATE_KEY=0x...` (optional: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`).
6. **Check.** `tripwire --config tripwire.yaml status` should show your testnet equity.
7. **Run.** Connect your agent via the MCP config above. In another terminal: `tripwire telegram` (buttons) or `tripwire pending` / `tripwire approve <id>`.

Mainnet requires `network: mainnet` **and** `TRIPWIRE_ALLOW_MAINNET=1`.

## Human CLI

```
tripwire status | policy | pending
tripwire approve <id> | deny <id>
tripwire reset-halt
tripwire audit show -n 50 | audit verify
tripwire telegram          # approve/deny from your phone
```

## Known limitations (v0)

- Daily-loss math counts deposits/withdrawals as P&L. Pause the agent around transfers.
- Enforcement lives in the Tripwire process. Anyone holding the master key can still trade directly. Keep that key off the agent's machine.
- Perps only (HyperCore validator perps). HIP-3 markets and spot aren't handled yet.
- Market orders use an IOC limit at mid ± `market_slippage`.

## Roadmap

On-chain policy enforcement on HyperEVM · per-agent budgets on one account · policy templates · hosted version for vault leaders.

## License

MIT
