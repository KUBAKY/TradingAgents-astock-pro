"""shortterm_history 落盘/解析/事后评估单元测试（mock 数据层，无网络）。"""

import pandas as pd
import pytest

import web.shortterm_history as sh


def _report(direction="买入", confidence="高"):
    return (f"**方向**: {direction}\n**置信度**: {confidence}\n"
            "### 逻辑链\n1. xxx\n### 交易计划\n- 介入条件: 回踩10日线")


def _stock_result(mode="swing", report=None):
    return {
        "mode": mode,
        "ch0": {"ticker": "000725", "name": "京东方A", "trade_date": "2026-07-01",
                "metrics": {"last_close": 10.0}},
        "report": report if report is not None else _report(),
        "bundle": "### 近期行情\n...",
    }


def _record(direction="买入", entry=10.0):
    return {
        "ticker": "000725", "trade_date": "2026-07-01",
        "ch0": {"metrics": {"last_close": entry}},
        "parsed": {"direction": direction, "confidence": "高"},
    }


def _df_after(bars):
    """bars: [(date, open, close), ...] 记录日之后的K线。"""
    return pd.DataFrame({
        "Date": pd.to_datetime([b[0] for b in bars]),
        "Open": [b[1] for b in bars],
        "High": [max(b[1], b[2]) for b in bars],
        "Low": [min(b[1], b[2]) for b in bars],
        "Close": [b[2] for b in bars],
        "Volume": [1000.0] * len(bars),
    })


def _mock_ohlcv(monkeypatch, bars):
    import tradingagents.dataflows.a_stock as a
    monkeypatch.setattr(a, "_load_ohlcv_astock", lambda code, asof: _df_after(bars))


class TestParseDecision:
    def test_full(self):
        p = sh.parse_decision(_report("卖出", "中"))
        assert p == {"direction": "卖出", "confidence": "中"}

    def test_missing(self):
        p = sh.parse_decision("# 无格式报告")
        assert p == {"direction": None, "confidence": None}

    def test_colon_variants(self):
        p = sh.parse_decision("**方向**：回避\n**置信度**：低")
        assert p == {"direction": "回避", "confidence": "低"}


class TestPersistence:
    def test_roundtrip(self, monkeypatch, tmp_path):
        monkeypatch.setattr(sh, "_DIR", tmp_path)
        path = sh.save_stock_record(_stock_result(), {"intent": "test"})
        assert path.exists()

        records = sh.list_records()
        assert len(records) == 1
        r = records[0]
        assert r["ticker"] == "000725" and r["kind"] == "stock"
        assert r["parsed"]["direction"] == "买入"
        assert "ch0" not in r  # 摘要不带全文

        full = sh.load_record(str(path))
        assert full["ch0"]["ticker"] == "000725"
        assert full["bundle"].startswith("### 近期行情")
        assert full["inputs"]["intent"] == "test"

    def test_ch0_only_skipped(self, monkeypatch, tmp_path):
        monkeypatch.setattr(sh, "_DIR", tmp_path)
        assert sh.save_stock_record(_stock_result(mode="ch0_only"), {}) is None
        assert sh.list_records() == []

    def test_filter_by_ticker_and_kind(self, monkeypatch, tmp_path):
        monkeypatch.setattr(sh, "_DIR", tmp_path)
        sh.save_stock_record(_stock_result(), {})
        sh.save_screen_record({"trade_date": "2026-07-01", "capital": 1e5,
                               "boards": {}, "rejected": {}}, "报告")
        assert len(sh.list_records()) == 2
        assert len(sh.list_records(ticker="000725")) == 1
        assert len(sh.list_records(kind="screen")) == 1


class TestEvaluateCall:
    BARS_UP = [("2026-07-02", 10.2, 10.3), ("2026-07-03", 10.3, 10.5),
               ("2026-07-06", 10.5, 11.0)]
    BARS_DOWN = [("2026-07-02", 9.8, 9.7), ("2026-07-03", 9.7, 9.5),
                 ("2026-07-06", 9.5, 9.0)]

    def test_buy_right(self, monkeypatch):
        _mock_ohlcv(monkeypatch, self.BARS_UP)
        ev = sh.evaluate_call(_record("买入"), asof_date="2026-07-06")
        assert ev["verdict"] == "对"
        assert ev["t1_open_pct"] == pytest.approx(2.0)
        assert ev["t1_close_pct"] == pytest.approx(3.0)
        assert ev["t3_close_pct"] == pytest.approx(10.0)
        assert ev["bars_after"] == 3

    def test_buy_wrong(self, monkeypatch):
        _mock_ohlcv(monkeypatch, self.BARS_DOWN)
        ev = sh.evaluate_call(_record("买入"), asof_date="2026-07-06")
        assert ev["verdict"] == "错"
        assert ev["t3_close_pct"] == pytest.approx(-10.0)

    def test_sell_right(self, monkeypatch):
        _mock_ohlcv(monkeypatch, self.BARS_DOWN)
        ev = sh.evaluate_call(_record("卖出"), asof_date="2026-07-06")
        assert ev["verdict"] == "对"

    def test_hold_not_scored(self, monkeypatch):
        _mock_ohlcv(monkeypatch, self.BARS_UP)
        ev = sh.evaluate_call(_record("观望"), asof_date="2026-07-06")
        assert ev["verdict"] == "不评分"
        assert ev["t3_close_pct"] is not None  # 收益仍展示

    def test_no_bars_pending(self, monkeypatch):
        _mock_ohlcv(monkeypatch, [])
        ev = sh.evaluate_call(_record("买入"), asof_date="2026-07-01")
        assert ev["verdict"] == "待验证"

    def test_insufficient_bars_pending(self, monkeypatch):
        _mock_ohlcv(monkeypatch, self.BARS_UP[:2])
        ev = sh.evaluate_call(_record("买入"), asof_date="2026-07-03")
        assert ev["verdict"] == "待验证"
        assert ev["t1_close_pct"] is not None
        assert ev["t3_close_pct"] is None

    def test_t10_computed(self, monkeypatch):
        bars = self.BARS_UP + [(f"2026-07-{7+i:02d}", 11.0, 11.0 + i * 0.1) for i in range(7)]
        _mock_ohlcv(monkeypatch, bars)
        ev = sh.evaluate_call(_record("买入"), asof_date="2026-07-13")
        assert ev["t10_close_pct"] is not None

    def test_missing_entry_close(self, monkeypatch):
        _mock_ohlcv(monkeypatch, self.BARS_UP)
        rec = _record("买入")
        rec["ch0"] = {"metrics": {}}
        ev = sh.evaluate_call(rec, asof_date="2026-07-06")
        assert ev["verdict"] == "不评分"
