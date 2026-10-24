"""组合风控测试：7.5 风险预算仓位 + 7.6 组合约束。"""

import pandas as pd
import pytest

from tradingagents.backtest.engine import BacktestEngine
from tradingagents.backtest.risk import RiskConfig, Signal, apply_risk, tier_of
from tradingagents.backtest.strategies import WeightWithStops


def test_capital_tier_mapping():
    assert tier_of(50_000)[1] == 0.020
    assert tier_of(200_000)[1] == 0.015
    assert tier_of(1_000_000)[1] == 0.010
    assert tier_of(5_000_000)[1] == 0.005
    assert tier_of(20_000_000)[1] == 0.003


def test_position_from_risk_budget():
    """R=1.5%，止损 8% → 18.75%；止损 20% → 7.5%。"""
    cfg = RiskConfig(capital=200_000, max_single_weight=0.30)
    w, notes = apply_risk([
        Signal("600519", weight=1.0, stop=0.08),
        Signal("000001", weight=1.0, stop=0.20),
    ], equity=200_000, cfg=cfg)

    assert w["600519"] == pytest.approx(0.1875, rel=1e-3)
    assert w["000001"] == pytest.approx(0.075, rel=1e-3)


def test_no_stop_means_no_position():
    cfg = RiskConfig(capital=1_000_000)
    w, notes = apply_risk([Signal("600519", weight=0.5, stop=None)], 1_000_000, cfg)
    assert w == {}
    assert any("无止损位" in n for n in notes)


def test_portfolio_risk_cap_scales_down():
    """按 R 定仓时每个持仓贡献风险恒为 R，因此 6% 上限等价于「最多 cap/R 只」。

    R=1%、止损 8% → 单票 12.5%；8 只 → 风险 8% > 6%，等比缩到 6%。
    """
    cfg = RiskConfig(capital=1_000_000, max_single_weight=0.30)
    signals = [Signal(f"S{i}", weight=0.25, stop=0.08) for i in range(8)]
    w, notes = apply_risk(signals, 1_000_000, cfg)

    total_risk = sum(weights * 0.08 for weights in w.values())
    assert total_risk == pytest.approx(0.06, rel=1e-3)
    assert len(w) == 8
    assert any("组合风险" in n for n in notes)


def test_industry_cap():
    cfg = RiskConfig(capital=1_000_000, max_single_weight=0.30, industry_cap=0.30)
    w, _ = apply_risk([
        Signal("A", weight=0.25, stop=0.05, industry="半导体"),
        Signal("B", weight=0.25, stop=0.05, industry="半导体"),
    ], 1_000_000, cfg)

    assert sum(w.values()) == pytest.approx(0.30, rel=1e-3)


def test_soft_veto_and_unstable_discount():
    cfg = RiskConfig(capital=1_000_000, max_single_weight=0.30)
    w, notes = apply_risk([
        Signal("A", weight=0.30, stop=0.10, soft_veto=True),
        Signal("B", weight=0.30, stop=0.10, unstable=True),
    ], 1_000_000, cfg)

    assert w["A"] <= 0.30 / 3 + 1e-9
    assert w["B"] == pytest.approx(0.10 * 0.5, rel=1e-2)
    assert any("软否决" in n for n in notes) and any("不稳定" in n for n in notes)


def _bars(symbols, dates, price=10.0):
    rows = []
    for s in symbols:
        for d in dates:
            rows.append({"symbol": s, "trade_date": d, "open": price, "high": price,
                         "low": price, "close": price, "volume": 1e6,
                         "pre_close": price})
    return pd.DataFrame(rows)


def test_engine_applies_risk_config():
    dates = ["2026-01-05", "2026-01-06", "2026-01-07"]
    df = _bars(["600519", "000001"], dates)
    cfg = RiskConfig(capital=200_000, max_single_weight=0.30)
    strategy = WeightWithStops({
        "600519": {"stop": 0.08, "industry": "白酒"},
        "000001": {"stop": 0.20, "industry": "银行"},
    }, weight_each=1.0)

    engine = BacktestEngine(df, cash=200_000, risk=cfg)
    res = engine.run(strategy)

    assert res.fills, "应有成交"
    invested = sum(f.amount for f in res.fills if f.side == "buy")
    # 两票合计约 26.25%（18.75% + 7.5%）
    assert invested / 200_000 == pytest.approx(0.2625, rel=0.05)
    assert res.risk_notes == [] or isinstance(res.risk_notes, list)
