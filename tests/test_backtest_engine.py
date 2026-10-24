"""回测引擎测试：撮合、T+1、涨跌停、停牌、费用与记帐。

全部用构造数据，不触网。
"""

import pandas as pd
import pytest

from tradingagents.backtest.engine import BacktestEngine, CostConfig
from tradingagents.backtest.metrics import compute
from tradingagents.backtest.strategies import BuyAndHold


def _bar(symbol, dates, prices, volumes=None, opens=None):
    """构造日线；prices 为收盘价序列。"""
    rows = []
    for i, d in enumerate(dates):
        close = prices[i]
        open_ = opens[i] if opens else close
        rows.append({
            "symbol": symbol, "trade_date": d, "open": open_, "high": open_,
            "low": open_, "close": close,
            "volume": (volumes[i] if volumes else 1_000_000.0),
            "pre_close": prices[i - 1] if i else None,
        })
    return pd.DataFrame(rows)


DATES = ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08"]


def test_buy_and_hold_zero_cost_matches_price_ratio():
    df = _bar("600000", DATES, [10.0, 10.0, 11.0, 12.0])
    cost = CostConfig(stamp_duty=0, commission=0, min_commission=0,
                      transfer_fee=0, slippage=0)
    engine = BacktestEngine(df, cash=100_000, cost=cost)
    res = engine.run(BuyAndHold(["600000"]))

    # T 日信号 → T+1 开盘（10.0）成交，最后一日收盘 12.0
    total = res.equity["equity"].iloc[-1] / 100_000 - 1
    assert 0.199 < total < 0.201


def test_t_plus_one_blocks_same_day_sell():
    """当日买入的股份 available 为 0，卖出信号只能等到次日。"""
    df = _bar("600000", DATES, [10.0, 10.0, 10.0, 10.0])

    class SellSameDay:
        def on_bar(self, date, history, positions, equity):
            if not positions:
                return {"600000": 1.0}
            return {}          # 立刻清仓

    engine = BacktestEngine(df, cash=100_000)
    res = engine.run(SellSameDay())
    sells = [f for f in res.fills if f.side == "sell"]
    buys = [f for f in res.fills if f.side == "buy"]
    assert buys, "应该先买入"
    # 买入发生在第 2 日开盘；卖出最快第 3 日开盘
    assert sells and sells[0].trade_date > buys[0].trade_date


def test_one_word_limit_up_blocks_buy():
    # 第 2 日一字涨停（open=high=low=pre_close*1.1）
    df = pd.DataFrame([
        {"symbol": "600000", "trade_date": DATES[0], "open": 10.0, "high": 10.0,
         "low": 10.0, "close": 10.0, "volume": 1e6, "pre_close": 10.0},
        {"symbol": "600000", "trade_date": DATES[1], "open": 11.0, "high": 11.0,
         "low": 11.0, "close": 11.0, "volume": 1e6, "pre_close": 10.0},
        {"symbol": "600000", "trade_date": DATES[2], "open": 11.0, "high": 11.0,
         "low": 11.0, "close": 11.0, "volume": 1e6, "pre_close": 11.0},
    ])
    engine = BacktestEngine(df, cash=100_000)
    res = engine.run(BuyAndHold(["600000"]))

    # 涨停当日不得成交；策略下一日继续追单，那是引擎的正确行为
    assert all(f.trade_date != DATES[1] for f in res.fills)
    assert any("涨停" in r["reason"] for r in res.rejected)


def test_suspension_blocks_trade():
    df = _bar("600000", DATES, [10.0, 10.0, 10.0, 10.0], volumes=[1e6, 0, 1e6, 1e6])
    engine = BacktestEngine(df, cash=100_000)
    res = engine.run(BuyAndHold(["600000"]))
    if res.fills:
        assert res.fills[0].trade_date != DATES[1]
    assert any("停牌" in r["reason"] for r in res.rejected)


def test_fees_follow_cost_model():
    df = _bar("600000", DATES, [10.0, 10.0, 10.0, 10.0])
    engine = BacktestEngine(df, cash=100_000)
    res = engine.run(BuyAndHold(["600000"]))
    buy = [f for f in res.fills if f.side == "buy"][0]
    expected = max(buy.amount * 0.00025, 5.0) + buy.amount * 0.00001
    assert buy.fee == pytest.approx(expected, rel=1e-9)
    # 买入价含 0.1% 滑点
    assert buy.price == pytest.approx(10.0 * 1.001, rel=1e-9)


def test_metrics_drawdown_and_trades():
    df = _bar("600000", DATES, [10.0, 12.0, 9.0, 11.0])
    engine = BacktestEngine(df, cash=100_000)
    res = engine.run(BuyAndHold(["600000"]))
    m = compute(res)

    assert m["days"] == len(DATES)
    assert m["max_drawdown"] < 0
    assert m["trades"] >= 1
    assert m["final_equity"] > 0


def test_segment_return_equal_weight():
    """环节等权：两只票 +100% / 0% → 平均 +50%；样本不足的标的剔除。"""
    from scripts.run_bottleneck import segment_return
    rows = []
    for i, d in enumerate([f"2026-01-{n:02d}" for n in range(1, 26)]):
        rows.append({"symbol": "A", "trade_date": d, "close": 10.0 + i, "open": 10.0,
                     "high": 10.0, "low": 10.0, "volume": 1e6, "pre_close": 10.0})
        rows.append({"symbol": "B", "trade_date": d, "close": 10.0, "open": 10.0,
                     "high": 10.0, "low": 10.0, "volume": 1e6, "pre_close": 10.0})
    df = pd.DataFrame(rows)
    ret, missing = segment_return(df, ["A", "B", "C"], "2026-01-01", "2026-01-25")
    assert missing == ["C"]
    assert ret == pytest.approx(((34 / 10 - 1) + 0) / 2, rel=1e-6)
