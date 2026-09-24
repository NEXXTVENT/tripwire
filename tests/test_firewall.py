import json

import pytest

from tripwire.approvals import ApprovalStore
from tripwire.audit import AuditLog
from tripwire.firewall import Firewall
from tripwire.models import OrderIntent
from tripwire.notify import NullNotifier, TelegramNotifier
from tripwire.policy import Policy, PolicyEngine
from tripwire.venue import HyperliquidVenue, MockVenue


class Clock:
    def __init__(self, t=1_790_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def setup(tmp_path):
    clock = Clock()
    venue = MockVenue(equity=10_000)
    notifier = NullNotifier()
    fw = Firewall(PolicyEngine(Policy()), venue, tmp_path, notifier=notifier, clock=clock)
    return fw, venue, notifier, clock


def test_allowed_order_executes_and_is_audited(setup):
    fw, venue, _, _ = setup
    r = fw.submit(OrderIntent("BTC", True, 0.003), reason="test entry")
    assert r["status"] == "executed"
    assert venue.positions["BTC"].szi == 0.003
    events = [e["event"] for e in fw.audit.entries()]
    assert events == ["order_request", "order_result"]
    assert fw.audit.entries()[0]["data"]["agent_reason"] == "test entry"


def test_denied_order_never_reaches_venue(setup):
    fw, venue, _, _ = setup
    r = fw.submit(OrderIntent("BTC", True, 1.0))  # $100k
    assert r["status"] == "denied"
    assert venue.fills == []


def test_check_is_dry_run(setup):
    fw, venue, _, _ = setup
    r = fw.check(OrderIntent("ETH", True, 0.1))
    assert r["verdict"] == "ALLOW"
    assert venue.fills == []


def test_approval_flow_single_use(setup):
    fw, venue, notifier, _ = setup
    r = fw.submit(OrderIntent("ETH", True, 0.2), reason="bigger")  # $800 > $500 threshold
    assert r["status"] == "pending_approval"
    rid = r["approval_id"]
    assert notifier.sent and notifier.sent[-1][1] == rid
    # Not approved yet -> cannot execute.
    assert fw.execute_approved(rid)["status"] == "error"
    fw.approvals.decide(rid, approve=True, by="test")
    assert fw.execute_approved(rid)["status"] == "executed"
    assert venue.positions["ETH"].szi == pytest.approx(0.2)
    # Cannot be spent twice.
    assert fw.execute_approved(rid)["status"] == "error"
    assert len(venue.fills) == 1


def test_denied_approval_cannot_execute(setup):
    fw, venue, _, _ = setup
    rid = fw.submit(OrderIntent("ETH", True, 0.2))["approval_id"]
    fw.approvals.decide(rid, approve=False, by="test")
    assert fw.execute_approved(rid)["status"] == "error"
    assert venue.fills == []


def test_approval_expires(setup):
    fw, _, _, clock = setup
    rid = fw.submit(OrderIntent("ETH", True, 0.2))["approval_id"]
    clock.t += 601
    assert fw.approvals.get(rid)["status"] == "expired"
    with pytest.raises(ValueError):
        fw.approvals.decide(rid, approve=True, by="late")


def test_approved_order_rechecked_at_execution(setup):
    fw, venue, _, _ = setup
    rid = fw.submit(OrderIntent("ETH", True, 0.2))["approval_id"]
    fw.approvals.decide(rid, approve=True, by="test")
    venue.set_mid("ETH", 20_000)  # price 5x -> order now $4,000 > max order
    r = fw.execute_approved(rid)
    assert r["status"] == "denied"
    assert venue.fills == []


def test_kill_switch_end_to_end(setup):
    fw, venue, notifier, clock = setup
    fw.engine.policy.require_approval_above_usd = None
    # Build a $1,800 BTC long in two $900 orders.
    for _ in range(2):
        assert fw.submit(OrderIntent("BTC", True, 0.009))["status"] == "executed"
    venue.set_mid("BTC", 70_000)  # -30% on 0.018 BTC = -$540 = -5.4% equity
    r = fw.submit(OrderIntent("ETH", True, 0.01), reason="add more")
    assert r["status"] == "denied" and "HALTED" in r["reasons"][0]
    assert any("HALT" in m for m, _ in notifier.sent)
    # De-risking is still allowed.
    assert fw.close_position("BTC", reason="stop the bleeding")["status"] == "executed"
    assert "BTC" not in venue.positions
    # Still halted until a human resets.
    assert fw.submit(OrderIntent("ETH", True, 0.01))["status"] == "denied"
    fw.reset_halt(by="test")
    assert fw.submit(OrderIntent("ETH", True, 0.01))["status"] == "executed"
    assert "halt" in [e["event"] for e in fw.audit.entries()]
    assert "halt_reset" in [e["event"] for e in fw.audit.entries()]


def test_close_position_noop(setup):
    fw, _, _, _ = setup
    assert fw.close_position("BTC")["status"] == "noop"


def test_venue_exception_is_audited(setup):
    fw, venue, _, _ = setup

    def boom(intent):
        raise ConnectionError("exchange down")

    venue.place = boom
    r = fw.submit(OrderIntent("BTC", True, 0.001))
    assert r["status"] == "error"
    assert "exchange down" in fw.audit.entries()[-1]["data"]["result"]["error"]


def test_state_persists_across_instances(tmp_path):
    clock = Clock()
    venue = MockVenue(equity=10_000)
    fw1 = Firewall(PolicyEngine(Policy()), venue, tmp_path, clock=clock)
    fw1.status()
    venue.cash = 9_000  # -10%
    fw1.status()
    fw2 = Firewall(PolicyEngine(Policy()), venue, tmp_path, clock=clock)
    assert fw2.status()["halted"] is True


# ---------------------------------------------------------------- audit log
def test_audit_chain_detects_tampering(tmp_path):
    log = AuditLog(tmp_path / "a.jsonl")
    for i in range(5):
        log.append("e", {"i": i})
    assert log.verify()[0]
    lines = (tmp_path / "a.jsonl").read_text().splitlines()
    row = json.loads(lines[2])
    row["data"]["i"] = 999
    lines[2] = json.dumps(row, sort_keys=True)
    (tmp_path / "a.jsonl").write_text("\n".join(lines) + "\n")
    ok, msg = log.verify()
    assert not ok and "seq 3" in msg


def test_audit_detects_deleted_line(tmp_path):
    log = AuditLog(tmp_path / "a.jsonl")
    for i in range(4):
        log.append("e", {"i": i})
    lines = (tmp_path / "a.jsonl").read_text().splitlines()
    del lines[1]
    (tmp_path / "a.jsonl").write_text("\n".join(lines) + "\n")
    assert not log.verify()[0]


# ---------------------------------------------------------------- telegram
def test_telegram_only_honors_configured_chat(tmp_path):
    calls = []
    tg = TelegramNotifier("T", "42", post=lambda url, p: calls.append((url, p)) or {})
    store = ApprovalStore(tmp_path / "ap.json")
    rid = store.create({"coin": "BTC"}, {}, "")["id"]
    evil = {"callback_query": {"id": "1", "data": f"approve:{rid}", "message": {"chat": {"id": 666}}}}
    assert tg.handle_update(evil, store) == "ignored: wrong chat"
    assert store.get(rid)["status"] == "pending"
    good = {"callback_query": {"id": "2", "data": f"approve:{rid}", "from": {"id": 7}, "message": {"chat": {"id": 42}}}}
    audit = AuditLog(tmp_path / "a.jsonl")
    assert tg.handle_update(good, store, audit).startswith("APPROVED")
    assert store.get(rid)["decided_by"] == "telegram:7"
    assert audit.entries()[-1]["event"] == "approval_approved"


def test_telegram_send_has_buttons():
    calls = []
    tg = TelegramNotifier("T", "42", post=lambda url, p: calls.append((url, p)) or {})
    tg.send("hi", approval_id="abc")
    kb = calls[0][1]["reply_markup"]["inline_keyboard"][0]
    assert kb[0]["callback_data"] == "approve:abc" and kb[1]["callback_data"] == "deny:abc"


def test_telegram_failure_does_not_break_orders():
    def fail(url, p):
        raise OSError("no network")
    TelegramNotifier("T", "42", post=fail).send("x", "y")  # must not raise


# ---------------------------------------------------------------- HL response parsing
def test_hl_parse_responses():
    ok_fill = {"status": "ok", "response": {"type": "order", "data": {"statuses": [
        {"filled": {"totalSz": "0.01", "avgPx": "100000", "oid": 1}}]}}}
    assert HyperliquidVenue._parse(ok_fill)["status"] == "filled"
    rest = {"status": "ok", "response": {"type": "order", "data": {"statuses": [{"resting": {"oid": 9}}]}}}
    assert HyperliquidVenue._parse(rest) ["oid"] == 9
    err = {"status": "ok", "response": {"type": "order", "data": {"statuses": [{"error": "Insufficient margin"}]}}}
    assert HyperliquidVenue._parse(err) ["status"] == "error"
    assert HyperliquidVenue._parse({"status": "err", "response": "bad sig"})["status"] == "error"
