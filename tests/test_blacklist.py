"""黑名单判定逻辑测试（构造数据，不触网）。"""

import pandas as pd

from scripts.build_blacklist import build
from tradingagents.backtest.store import Warehouse


def _seed(tmp_path, rows, pool):
    wh = Warehouse(tmp_path)
    wh.ensure_schema()
    wh.write_kline(pd.DataFrame(rows))
    wh.upsert_pool(pd.DataFrame(pool))
    return wh


def _kline(sym, n=70, price=10.0, volume=1e6, amount=1e8):
    dates = pd.bdate_range("2026-06-01", periods=n).strftime("%Y-%m-%d")
    return [{"symbol": sym, "trade_date": d, "open": price, "high": price,
             "low": price, "close": price, "volume": volume, "amount": amount,
             "source": "test"} for d in dates]


def test_st_and_low_price_flagged(tmp_path):
    rows = _kline("600001", price=1.2)
    rows[-1]["volume"] = 0            # 最新日停牌
    wh = _seed(tmp_path, rows, [{"trade_date": rows[-1]["trade_date"],
                                 "symbol": "600001", "name": "*ST测试", "board": "沪主板"}])
    res, latest = build(wh)
    d = res["symbols"]["600001"]
    assert any("ST" in r for r in d["reasons"])
    assert any("面值" in r for r in d["reasons"])
    assert any("停牌" in r for r in d["reasons"])


def test_low_liquidity_flagged(tmp_path):
    rows = _kline("600002", amount=1e6)   # 100 万 < 1000 万
    wh = _seed(tmp_path, rows, [{"trade_date": rows[-1]["trade_date"],
                                 "symbol": "600002", "name": "低流动性", "board": "沪主板"}])
    res, _ = build(wh)
    assert any("流动性" in r for r in res["symbols"]["600002"]["reasons"])


def test_new_stock_flagged(tmp_path):
    # 需要一只"老票"把 60 日窗口撑开，新票只出现在窗口后半段
    old = _kline("600000", n=70)
    new = [dict(r, symbol="600003") for r in old[-20:]]
    rows = old + new
    pool = [{"trade_date": old[-1]["trade_date"], "symbol": "600000",
             "name": "参考老票", "board": "沪主板"},
            {"trade_date": new[-1]["trade_date"], "symbol": "600003",
             "name": "次新", "board": "沪主板"}]
    wh = _seed(tmp_path, rows, pool)
    res, _ = build(wh)
    assert any("次新" in r for r in res["symbols"]["600003"]["reasons"])


def test_clean_stock_not_flagged(tmp_path):
    rows = _kline("600004")
    wh = _seed(tmp_path, rows, [{"trade_date": rows[-1]["trade_date"],
                                 "symbol": "600004", "name": "正常公司", "board": "沪主板"}])
    res, _ = build(wh)
    assert "600004" not in res["symbols"]
