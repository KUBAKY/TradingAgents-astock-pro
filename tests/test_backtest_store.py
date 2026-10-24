"""回测数据仓存储层测试（不触网）。

覆盖：幂等写入、按日过滤、股票池快照、财务 PIT 可见性。
"""

import pandas as pd

from tradingagents.backtest.store import Warehouse


def _kline(symbols, dates, close=10.0):
    rows = []
    for s in symbols:
        for i, d in enumerate(dates):
            rows.append({"symbol": s, "trade_date": d, "open": close, "high": close,
                         "low": close, "close": close + i, "volume": 1000.0,
                         "source": "test"})
    return pd.DataFrame(rows)


def test_write_kline_is_idempotent(tmp_path):
    wh = Warehouse(tmp_path)
    wh.ensure_schema()
    df = _kline(["600519", "000001"], ["2026-10-22", "2026-10-23"])

    assert wh.write_kline(df) == 4
    wh.write_kline(df)
    out = wh.read_kline()

    assert len(out) == 4
    assert sorted(out["trade_date"].unique()) == ["2026-10-22", "2026-10-23"]


def test_write_kline_keeps_latest_on_conflict(tmp_path):
    wh = Warehouse(tmp_path)
    wh.ensure_schema()
    wh.write_kline(_kline(["600519"], ["2026-10-23"], close=10.0))
    wh.write_kline(_kline(["600519"], ["2026-10-23"], close=99.0))

    out = wh.read_kline()
    assert len(out) == 1
    assert out.iloc[0]["open"] == 99.0


def test_read_kline_filters_by_symbol_and_date(tmp_path):
    wh = Warehouse(tmp_path)
    wh.ensure_schema()
    wh.write_kline(_kline(["600519", "000001"],
                          ["2026-10-21", "2026-10-22", "2026-10-23"]))

    out = wh.read_kline(symbols=["600519"], start="2026-10-22")
    assert set(out["symbol"]) == {"600519"}
    assert out["trade_date"].min() == "2026-10-22"
    assert len(out) == 2


def test_pool_asof_returns_only_that_day(tmp_path):
    wh = Warehouse(tmp_path)
    wh.ensure_schema()
    wh.upsert_pool(pd.DataFrame([
        {"trade_date": "2026-10-22", "symbol": "600519", "name": "贵州茅台",
         "board": "沪主板", "is_st": 0},
        {"trade_date": "2026-10-23", "symbol": "600519", "name": "贵州茅台",
         "board": "沪主板", "is_st": 0},
        {"trade_date": "2026-10-23", "symbol": "000001", "name": "平安银行",
         "board": "深主板", "is_st": 0},
    ]))

    assert len(wh.pool_asof("2026-10-22")) == 1
    assert len(wh.pool_asof("2026-10-23")) == 2
    assert wh.pool_asof("2026-10-21").empty


def test_financial_asof_blocks_future_publish_date(tmp_path):
    wh = Warehouse(tmp_path)
    wh.ensure_schema()
    wh.upsert_financial(pd.DataFrame([
        # 2024Q1：4 月底才公告
        {"symbol": "600519", "report_period": "2024-03-31",
         "publish_date": "2024-04-29", "item": "revenue", "value": 100.0},
        # 2023 年报：3 月底公告
        {"symbol": "600519", "report_period": "2023-12-31",
         "publish_date": "2024-03-29", "item": "revenue", "value": 90.0},
    ]))

    early = wh.financial_asof("600519", "2024-04-10")
    assert list(early["report_period"]) == ["2023-12-31"]

    late = wh.financial_asof("600519", "2024-04-30")
    assert len(late) == 2


def test_write_kline_drops_non_positive_prices(tmp_path):
    """新浪对"代码启用前的历史日期"会返回 0 价占位行（实测 920xxx 2018–2020），必须丢弃。"""
    wh = Warehouse(tmp_path)
    wh.ensure_schema()
    good = _kline(["600519"], ["2026-10-23"])
    bad = _kline(["920000"], ["2019-05-23"]).assign(open=0.0, high=0.0, low=0.0, close=0.0)

    assert wh.write_kline(pd.concat([good, bad], ignore_index=True)) == 1
    out = wh.read_kline()
    assert len(out) == 1
    assert out.iloc[0]["symbol"] == "600519"


def test_stats_counts_rows_and_sizes(tmp_path):
    wh = Warehouse(tmp_path)
    wh.ensure_schema()
    wh.write_kline(_kline(["600519"], ["2026-10-23"]))

    st = wh.stats()
    assert st["kline_files"] == 1
    assert st["kline_rows"] == 1
    assert st["kline_bytes"] > 0
