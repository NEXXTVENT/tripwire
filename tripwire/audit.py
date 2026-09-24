"""Append-only, hash-chained audit log (JSONL).

Every line carries the hash of the previous line, so deleting or editing any past
entry breaks the chain and `verify()` reports exactly where.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Callable

GENESIS = "0" * 64


def _digest(entry: dict) -> str:
    body = {k: v for k, v in entry.items() if k != "hash"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


class AuditLog:
    def __init__(self, path: str | Path, clock: Callable[[], float] = time.time):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock

    def _tail(self) -> tuple[int, str]:
        if not self.path.exists():
            return 0, GENESIS
        last = None
        with self.path.open("rb") as f:
            for line in f:
                if line.strip():
                    last = line
        if last is None:
            return 0, GENESIS
        e = json.loads(last)
        return e["seq"], e["hash"]

    def append(self, event: str, data: dict[str, Any]) -> dict:
        seq, prev = self._tail()
        entry = {"seq": seq + 1, "ts": self.clock(), "event": event, "data": data, "prev_hash": prev}
        entry["hash"] = _digest(entry)
        with self.path.open("a") as f:
            f.write(json.dumps(entry, sort_keys=True, default=str) + "\n")
        return entry

    def entries(self, last: int | None = None) -> list[dict]:
        if not self.path.exists():
            return []
        rows = [json.loads(l) for l in self.path.read_text().splitlines() if l.strip()]
        return rows[-last:] if last else rows

    def verify(self) -> tuple[bool, str]:
        prev = GENESIS
        for i, e in enumerate(self.entries(), start=1):
            if e.get("seq") != i:
                return False, f"sequence gap at line {i} (seq={e.get('seq')})"
            if e.get("prev_hash") != prev:
                return False, f"chain broken at seq {i}: prev_hash mismatch"
            if _digest(e) != e.get("hash"):
                return False, f"entry seq {i} was modified (hash mismatch)"
            prev = e["hash"]
        return True, f"ok: {len(self.entries())} entries, chain intact"
