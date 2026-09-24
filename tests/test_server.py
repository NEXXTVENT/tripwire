"""The agent-facing tool surface must not include any way to weaken the guardrails."""

import asyncio

from tripwire import server


def tool_names():
    tools = asyncio.run(server.mcp.list_tools())
    return {t.name for t in tools}


def test_exposed_tools_are_exactly_the_guarded_set():
    assert tool_names() == {
        "get_status", "get_policy", "check_order", "place_order", "close_position",
        "cancel_order", "get_approval", "execute_approved", "get_audit_log",
    }


def test_no_dangerous_tools():
    for name in tool_names():
        for bad in ("withdraw", "transfer", "reset", "approve_order", "set_policy", "update_policy", "leverage"):
            assert bad not in name


def test_tools_work_end_to_end(tmp_path, monkeypatch):
    (tmp_path / "tripwire.yaml").write_text("venue: mock\ndata_dir: data\npolicy: policy.yaml\n")
    (tmp_path / "policy.yaml").write_text("allowed_markets: [BTC]\nrequire_approval_above_usd: null\n")
    monkeypatch.setenv("TRIPWIRE_CONFIG", str(tmp_path / "tripwire.yaml"))
    monkeypatch.setattr(server, "_fw", None)
    assert server.check_order("BTC", "buy", 0.001)["verdict"] == "ALLOW"
    assert server.place_order("BTC", "buy", 0.001, reason="t")["status"] == "executed"
    assert server.place_order("ETH", "buy", 0.1, reason="t")["status"] == "denied"
    assert server.get_status()["account"]["positions"]["BTC"]["szi"] == 0.001
    assert server.close_position("BTC", reason="done")["status"] == "executed"
    assert len(server.get_audit_log(50)) >= 5
