"""The firewall: snapshot → policy → audit → (approve) → execute."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable, Optional

from .approvals import APPROVED, ApprovalStore
from .filelock import locked
from .audit import AuditLog
from .models import OrderIntent, Verdict
from .notify import NullNotifier, Notifier
from .policy import PolicyEngine
from .state import StateStore
from .venue import Venue


class Firewall:
    def __init__(
        self,
        engine: PolicyEngine,
        venue: Venue,
        data_dir: str | Path,
        notifier: Optional[Notifier] = None,
        clock: Callable[[], float] = time.time,
        approval_ttl: int = 600,
    ):
        self.engine = engine
        self.venue = venue
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self.state_store = StateStore(self.data_dir / "state.json")
        self.audit = AuditLog(self.data_dir / "audit.jsonl", clock=clock)
        self.approvals = ApprovalStore(self.data_dir / "approvals.json", ttl_seconds=approval_ttl, clock=clock)
        self.notifier = notifier or NullNotifier()

    def _locked(self):
        """Serialize state changes across processes (MCP server, CLI, Telegram bot)."""
        return locked(self.data_dir / ".lock")

    # ------------------------------------------------------------------ reads
    def status(self) -> dict:
        with self._locked():
            state = self.state_store.load()
            snap = self.venue.snapshot()
            self.engine.observe(snap, state, self.clock())
            self.state_store.save(state)
        return {
            "account": snap.to_dict(),
            "halted": state.halted,
            "halt_reason": state.halt_reason,
            "day_start_equity": state.day_start_equity,
            "high_water_mark": state.high_water_mark,
            "orders_last_minute": state.orders_in_last(60, self.clock()),
            "pending_approvals": len(self.approvals.list("pending")),
        }

    def check(self, intent: OrderIntent) -> dict:
        """Dry run: what would the firewall say? Nothing is sent."""
        intent = self.venue.normalize(intent)
        with self._locked():
            state = self.state_store.load()
            snap = self.venue.snapshot()
            now = self.clock()
            self.engine.observe(snap, state, now)
            decision = self.engine.evaluate(intent, snap, state, now)
            self.state_store.save(state)
        self.audit.append("check", {"intent": intent.to_dict(), "decision": decision.to_dict()})
        return {"intent": intent.to_dict(), **decision.to_dict()}

    # ----------------------------------------------------------------- writes
    def submit(self, intent: OrderIntent, reason: str = "") -> dict:
        intent = self.venue.normalize(intent)
        with self._locked():
            state = self.state_store.load()
            snap = self.venue.snapshot()
            now = self.clock()
            was_halted = state.halted
            self.engine.observe(snap, state, now)
            if state.halted and not was_halted:
                self.audit.append("halt", {"reason": state.halt_reason, "equity": snap.equity})
                self.notifier.send(f"🛑 Tripwire HALT: {state.halt_reason}. Only de-risking allowed until reset.")
            decision = self.engine.evaluate(intent, snap, state, now)
            self.audit.append(
                "order_request",
                {"intent": intent.to_dict(), "agent_reason": reason, "decision": decision.to_dict()},
            )

            if decision.verdict == Verdict.DENY:
                self.state_store.save(state)
                return {"status": "denied", "intent": intent.to_dict(), **decision.to_dict()}

            if decision.verdict == Verdict.NEEDS_APPROVAL:
                rec = self.approvals.create(intent.to_dict(), decision.to_dict(), reason)
                self.state_store.save(state)
                self.audit.append("approval_requested", {"approval_id": rec["id"]})
                side = "BUY" if intent.is_buy else "SELL"
                self.notifier.send(
                    f"⚠️ Approval needed [{rec['id']}]\n{side} {intent.size} {intent.coin} "
                    f"(~${decision.metrics.get('order_notional_usd', 0):,.0f}, "
                    f"lev after {decision.metrics.get('leverage_after')}x)\nAgent says: {reason or '-'}",
                    approval_id=rec["id"],
                )
                return {
                    "status": "pending_approval",
                    "approval_id": rec["id"],
                    "intent": intent.to_dict(),
                    **decision.to_dict(),
                    "next": "Poll get_approval(approval_id); when approved call execute_approved(approval_id).",
                }

            return self._execute(intent, state, now, decision.to_dict())

    def execute_approved(self, approval_id: str) -> dict:
        with self._locked():  # held through execution so an approval can only be spent once
            rec = self.approvals.get(approval_id)
            if rec is None:
                return {"status": "error", "error": f"no approval {approval_id}"}
            if rec["status"] != APPROVED:
                return {"status": "error", "error": f"approval {approval_id} is {rec['status']}"}
            intent = OrderIntent(**rec["intent"])
            state = self.state_store.load()
            snap = self.venue.snapshot()
            now = self.clock()
            self.engine.observe(snap, state, now)
            # Re-check at execution time: the market may have moved since approval.
            decision = self.engine.evaluate(intent, snap, state, now, approved=True)
            self.audit.append("approved_recheck", {"approval_id": approval_id, "decision": decision.to_dict()})
            if decision.verdict != Verdict.ALLOW:
                self.state_store.save(state)
                return {"status": "denied", "approval_id": approval_id, **decision.to_dict()}
            result = self._execute(intent, state, now, decision.to_dict())
            self.approvals.mark_executed(approval_id, result)
        return result

    def close_position(self, coin: str, reason: str = "") -> dict:
        snap = self.venue.snapshot()
        szi = snap.position_size(coin.upper())
        if szi == 0:
            return {"status": "noop", "reason": f"no open {coin.upper()} position"}
        return self.submit(OrderIntent(coin.upper(), is_buy=szi < 0, size=abs(szi), reduce_only=True), reason)

    def cancel(self, coin: str, oid: int, reason: str = "") -> dict:
        # Cancelling never adds risk, so it's always allowed, but still audited.
        result = self.venue.cancel(coin, oid)
        self.audit.append("cancel", {"coin": coin, "oid": oid, "agent_reason": reason, "result": result})
        return result

    # ---------------------------------------------------------------- human-only
    def reset_halt(self, by: str) -> dict:
        with self._locked():
            state = self.state_store.load()
            snap = self.venue.snapshot()
            prev = state.halt_reason
            state.reset_halt(equity=snap.equity)
            self.state_store.save(state)
        self.audit.append("halt_reset", {"by": by, "previous_reason": prev, "equity": snap.equity})
        return {"status": "reset", "previous_reason": prev, "rebaselined_equity": snap.equity}

    # ------------------------------------------------------------------ internal
    def _execute(self, intent: OrderIntent, state, now: float, decision: dict) -> dict[str, Any]:
        try:
            result = self.venue.place(intent)
        except Exception as e:  # venue failure is logged, never swallowed silently
            result = {"status": "error", "error": f"{type(e).__name__}: {e}"}
        if result.get("status") != "error":
            state.record_order(now)
        self.state_store.save(state)
        safe = {k: v for k, v in result.items() if k != "raw"}
        self.audit.append("order_result", {"intent": intent.to_dict(), "result": safe})
        return {"status": "executed" if result.get("status") != "error" else "error",
                "result": safe, "intent": intent.to_dict(), "decision": decision}
