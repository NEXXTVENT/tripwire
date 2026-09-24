"""Human approval queue (JSON file shared by the MCP server, CLI and Telegram bot)."""

from __future__ import annotations

import json
import os
import secrets
import time
from pathlib import Path
from typing import Callable, Optional

from .filelock import locked

PENDING, APPROVED, DENIED, EXECUTED, EXPIRED = "pending", "approved", "denied", "executed", "expired"


class ApprovalStore:
    def __init__(self, path: str | Path, ttl_seconds: int = 600, clock: Callable[[], float] = time.time):
        self.path = Path(path)
        self.ttl = ttl_seconds
        self.clock = clock

    def _locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        return locked(self.path.with_suffix(".lock"))

    def _load(self) -> dict[str, dict]:
        if not self.path.exists():
            return {}
        return json.loads(self.path.read_text())

    def _save(self, data: dict[str, dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(f".tmp.{os.getpid()}")
        tmp.write_text(json.dumps(data, indent=2))
        os.replace(tmp, self.path)

    def _expire(self, data: dict[str, dict]) -> None:
        now = self.clock()
        for r in data.values():
            if r["status"] in (PENDING, APPROVED) and now - r["created"] > self.ttl:
                r["status"] = EXPIRED

    def _create(self, intent: dict, decision: dict, reason: str = "") -> dict:
        data = self._load()
        rid = secrets.token_hex(4)
        rec = {"id": rid, "intent": intent, "decision": decision, "agent_reason": reason,
               "status": PENDING, "created": self.clock(), "decided_by": None}
        data[rid] = rec
        self._save(data)
        return rec

    def _get(self, rid: str) -> Optional[dict]:
        data = self._load()
        self._expire(data)
        self._save(data)
        return data.get(rid)

    def _list(self, status: Optional[str] = None) -> list[dict]:
        data = self._load()
        self._expire(data)
        self._save(data)
        return [r for r in data.values() if status is None or r["status"] == status]

    def _decide(self, rid: str, approve: bool, by: str) -> dict:
        data = self._load()
        self._expire(data)
        rec = data.get(rid)
        if rec is None:
            raise KeyError(f"no approval request {rid}")
        if rec["status"] != PENDING:
            raise ValueError(f"request {rid} is {rec['status']}, not pending")
        rec["status"] = APPROVED if approve else DENIED
        rec["decided_by"] = by
        rec["decided_at"] = self.clock()
        self._save(data)
        return rec

    def _mark_executed(self, rid: str, result: dict) -> None:
        data = self._load()
        data[rid]["status"] = EXECUTED
        data[rid]["result"] = result
        self._save(data)

    # Public, cross-process-safe wrappers.
    def create(self, intent: dict, decision: dict, reason: str = "") -> dict:
        with self._locked():
            return self._create(intent, decision, reason)

    def get(self, rid: str) -> Optional[dict]:
        with self._locked():
            return self._get(rid)

    def list(self, status: Optional[str] = None) -> list[dict]:
        with self._locked():
            return self._list(status)

    def decide(self, rid: str, approve: bool, by: str) -> dict:
        with self._locked():
            return self._decide(rid, approve, by)

    def mark_executed(self, rid: str, result: dict) -> None:
        with self._locked():
            self._mark_executed(rid, result)
