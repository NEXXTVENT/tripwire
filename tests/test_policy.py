import pytest

from tripwire.models import AccountSnapshot, OrderIntent, Position, Verdict
from tripwire.policy import Policy, PolicyEngine, is_risk_reducing
from tripwire.state import RiskState

NOW = 1_790_000_000.0  # fixed clock


def snap(equity=10_000.0, positions=None, mids=None):
    return AccountSnapshot(
        equity=equity,
        positions=positions or {},
        mids=mids or {"BTC": 100_000.0, "ETH": 4_000.0, "DOGE": 0.2},
    )


def engine(**kw):
    base = dict(require_approval_above_usd=None)
    base.update(kw)
    return PolicyEngine(Policy(**base))


def test_small_order_allowed():
    d = engine().evaluate(OrderIntent("BTC", True, 0.005), snap(), RiskState(), NOW)
    assert d.verdict == Verdict.ALLOW


def test_market_not_allowed():
    d = engine().evaluate(OrderIntent("DOGE", True, 100), snap(), RiskState(), NOW)
    assert d.verdict == Verdict.DENY
    assert "not in allowed_markets" in d.reasons[0]


def test_zero_size_denied():
    d = engine().evaluate(OrderIntent("BTC", True, 0), snap(), RiskState(), NOW)
    assert d.verdict == Verdict.DENY


def test_missing_mid_fails_closed():
    s = snap(mids={"ETH": 4000.0})
    d = engine().evaluate(OrderIntent("BTC", True, 0.001), s, RiskState(), NOW)
    assert d.verdict == Verdict.DENY
    assert "no mid price" in d.reasons[0]


def test_order_notional_cap():
    d = engine().evaluate(OrderIntent("BTC", True, 0.02), snap(), RiskState(), NOW)  # $2,000
    assert d.verdict == Verdict.DENY
    assert any("order notional" in r for r in d.reasons)


def test_position_cap_counts_existing_position():
    s = snap(positions={"BTC": Position("BTC", 0.02)})  # already $2,000 long
    d = engine().evaluate(OrderIntent("BTC", True, 0.009), s, RiskState(), NOW)  # +$900 -> $2,900
    assert d.verdict == Verdict.DENY
    assert any("position after trade" in r for r in d.reasons)


def test_leverage_cap():
    s = snap(equity=1_000.0)
    d = engine(max_leverage=2.0).evaluate(OrderIntent("ETH", True, 0.6), s, RiskState(), NOW)  # $2,400 on $1k
    assert d.verdict == Verdict.DENY
    assert any("leverage" in r for r in d.reasons)


def test_gross_exposure_cap():
    s = snap(positions={"BTC": Position("BTC", 0.025), "ETH": Position("ETH", -0.6)})  # 2500 + 2400
    d = engine().evaluate(OrderIntent("BTC", False, 0.002), s, RiskState(), NOW)
    # Selling BTC reduces BTC -> allowed even though gross is near cap
    assert d.verdict == Verdict.ALLOW
    d = engine().evaluate(OrderIntent("ETH", False, 0.05), s, RiskState(), NOW)  # grows ETH short
    assert d.verdict == Verdict.DENY
    assert any("gross exposure" in r for r in d.reasons)


def test_fat_finger_limit_price():
    i = OrderIntent("BTC", True, 0.001, order_type="limit", limit_px=110_000)
    d = engine().evaluate(i, snap(), RiskState(), NOW)
    assert d.verdict == Verdict.DENY
    assert any("from mid" in r for r in d.reasons)


def test_limit_without_price_denied():
    i = OrderIntent("BTC", True, 0.001, order_type="limit")
    assert engine().evaluate(i, snap(), RiskState(), NOW).verdict == Verdict.DENY


def test_rate_limit():
    st = RiskState(order_times=[NOW - i for i in range(10)])
    d = engine().evaluate(OrderIntent("BTC", True, 0.001), snap(), st, NOW)
    assert d.verdict == Verdict.DENY
    assert any("rate limit" in r for r in d.reasons)


def test_approval_threshold():
    e = engine(require_approval_above_usd=500.0)
    i = OrderIntent("BTC", True, 0.008)  # $800
    assert e.evaluate(i, snap(), RiskState(), NOW).verdict == Verdict.NEEDS_APPROVAL
    assert e.evaluate(i, snap(), RiskState(), NOW, approved=True).verdict == Verdict.ALLOW


def test_approval_never_overrides_hard_limit():
    e = engine(require_approval_above_usd=500.0)
    i = OrderIntent("BTC", True, 0.05)  # $5,000 > max order
    assert e.evaluate(i, snap(), RiskState(), NOW, approved=True).verdict == Verdict.DENY


def test_daily_loss_halts_and_latches():
    e = engine(daily_loss_limit_pct=5.0)
    st = RiskState()
    e.observe(snap(equity=10_000), st, NOW)
    e.observe(snap(equity=9_400), st, NOW + 60)  # -6%
    assert st.halted and "daily loss" in st.halt_reason
    # Equity recovers: still halted until a human resets.
    e.observe(snap(equity=10_500), st, NOW + 120)
    assert st.halted
    d = e.evaluate(OrderIntent("BTC", True, 0.001), snap(), st, NOW + 120)
    assert d.verdict == Verdict.DENY and "HALTED" in d.reasons[0]


def test_halt_still_allows_derisking():
    e = engine()
    st = RiskState()
    st.halt("test", NOW)
    s = snap(positions={"BTC": Position("BTC", 0.02)})
    assert e.evaluate(OrderIntent("BTC", False, 0.01), s, st, NOW).verdict == Verdict.ALLOW
    # Flipping short is not de-risking.
    assert e.evaluate(OrderIntent("BTC", False, 0.03), s, st, NOW).verdict == Verdict.DENY


def test_drawdown_from_high_water_mark():
    e = engine(daily_loss_limit_pct=50, max_drawdown_pct=10)
    st = RiskState()
    e.observe(snap(equity=10_000), st, NOW)
    e.observe(snap(equity=12_000), st, NOW + 60)
    e.observe(snap(equity=10_700), st, NOW + 120)  # -10.8% from 12k
    assert st.halted and "drawdown" in st.halt_reason


def test_reset_rebaselines():
    st = RiskState()
    st.halt("x", NOW)
    st.reset_halt(equity=9_000)
    assert not st.halted and st.day_start_equity == 9_000 and st.high_water_mark == 9_000


def test_new_day_resets_day_start():
    e = engine()
    st = RiskState()
    e.observe(snap(equity=10_000), st, NOW)
    e.observe(snap(equity=9_700), st, NOW + 86_400)
    assert st.day_start_equity == 9_700


def test_reduce_only_must_reduce():
    i = OrderIntent("BTC", True, 0.001, reduce_only=True)
    d = engine().evaluate(i, snap(), RiskState(), NOW)
    assert d.verdict == Verdict.DENY


def test_is_risk_reducing():
    s = snap(positions={"ETH": Position("ETH", -1.0)})
    assert is_risk_reducing(OrderIntent("ETH", True, 0.5), s)
    assert is_risk_reducing(OrderIntent("ETH", True, 1.0), s)
    assert not is_risk_reducing(OrderIntent("ETH", True, 1.5), s)
    assert not is_risk_reducing(OrderIntent("ETH", False, 0.1), s)


def test_policy_rejects_unknown_keys(tmp_path):
    f = tmp_path / "p.yaml"
    f.write_text("max_levrage: 2\n")
    with pytest.raises(ValueError, match="Unknown policy keys"):
        Policy.load(f)


def test_policy_rejects_nonpositive(tmp_path):
    f = tmp_path / "p.yaml"
    f.write_text("max_leverage: 0\n")
    with pytest.raises(ValueError):
        Policy.load(f)


def test_empty_account_then_funded_rebaselines():
    e = engine(daily_loss_limit_pct=5.0)
    st = RiskState()
    e.observe(snap(equity=0.0), st, NOW)
    e.observe(snap(equity=1_000.0), st, NOW + 60)
    assert st.day_start_equity == 1_000.0 and st.high_water_mark == 1_000.0
    e.observe(snap(equity=940.0), st, NOW + 120)
    assert st.halted


def test_zero_equity_denies_new_risk():
    d = engine().evaluate(OrderIntent("BTC", True, 0.001), snap(equity=0.0), RiskState(), NOW)
    assert d.verdict == Verdict.DENY
