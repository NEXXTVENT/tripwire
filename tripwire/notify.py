"""Telegram notifications with Approve/Deny buttons. Stdlib only."""

from __future__ import annotations

import json
import time
import urllib.request
from typing import Any, Callable, Optional

HttpPost = Callable[[str, dict], dict]


def _post(url: str, payload: dict) -> dict:
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=40) as r:
        return json.loads(r.read())


class Notifier:
    def send(self, text: str, approval_id: Optional[str] = None) -> None: ...


class NullNotifier(Notifier):
    def __init__(self):
        self.sent: list[tuple[str, Optional[str]]] = []

    def send(self, text: str, approval_id: Optional[str] = None) -> None:
        self.sent.append((text, approval_id))


class TelegramNotifier(Notifier):
    def __init__(self, token: str, chat_id: str, post: HttpPost = _post):
        self.base = f"https://api.telegram.org/bot{token}"
        self.chat_id = str(chat_id)
        self.post = post

    def send(self, text: str, approval_id: Optional[str] = None) -> None:
        payload: dict[str, Any] = {"chat_id": self.chat_id, "text": text}
        if approval_id:
            payload["reply_markup"] = {"inline_keyboard": [[
                {"text": "✅ Approve", "callback_data": f"approve:{approval_id}"},
                {"text": "❌ Deny", "callback_data": f"deny:{approval_id}"},
            ]]}
        try:
            self.post(f"{self.base}/sendMessage", payload)
        except Exception:
            pass  # notifications must never break order handling

    def handle_update(self, update: dict, approvals, audit=None) -> Optional[str]:
        """Process one Telegram update. Only callbacks from the configured chat are honored."""
        cq = update.get("callback_query")
        if not cq:
            return None
        chat = str(cq.get("message", {}).get("chat", {}).get("id"))
        if chat != self.chat_id:
            return "ignored: wrong chat"
        action, _, rid = (cq.get("data") or "").partition(":")
        if action not in ("approve", "deny"):
            return "ignored: bad action"
        try:
            by = f"telegram:{cq.get('from', {}).get('id')}"
            rec = approvals.decide(rid, approve=(action == "approve"), by=by)
            msg = f"{rec['status'].upper()} {rid}"
            if audit is not None:
                audit.append(f"approval_{rec['status']}", {"approval_id": rid, "by": by})
        except (KeyError, ValueError) as e:
            msg = str(e)
        try:
            self.post(f"{self.base}/answerCallbackQuery", {"callback_query_id": cq.get("id"), "text": msg})
        except Exception:
            pass
        return msg

    def poll_forever(self, approvals, audit=None, log: Callable[[str], None] = print) -> None:
        offset = 0
        while True:
            try:
                r = self.post(f"{self.base}/getUpdates", {"offset": offset, "timeout": 30})
            except Exception as e:  # network hiccup: keep going
                log(f"poll error: {e}")
                time.sleep(5)
                continue
            for u in r.get("result", []):
                offset = u["update_id"] + 1
                out = self.handle_update(u, approvals, audit)
                if out:
                    log(out)
