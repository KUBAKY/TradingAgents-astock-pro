"""shortterm_history 落盘/解析/事后评估单元测试（mock 数据层，无网络）。"""

import pandas as pd
import pytest

import tradingagents.shortterm.history as sh


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


def _df_after_hl(bars):
    """bars: [(date, open, high, low, close), ...] 显式高低轴。"""
    return pd.DataFrame({
        "Date": pd.to_datetime([b[0] for b in bars]),
        "Open": [b[1] for b in bars],
        "High": [b[2] for b in bars],
        "Low": [b[3] for b in bars],
        "Close": [b[4] for b in bars],
        "Volume": [1000.0] * len(bars),
    })


def _mock_ohlcv_hl(monkeypatch, bars):
    import tradingagents.dataflows.a_stock as a
    monkeypatch.setattr(a, "_load_ohlcv_astock", lambda code, asof: _df_after_hl(bars))


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


class TestParsePriceLevels:
    def test_absolute_prices(self):
        rep = "### 交易计划\n- 目标价: 12.5（第一目标位）\n- 止损位: 9.8（跌破离场）"
        lv = sh.parse_price_levels(rep)
        assert lv["target"] == ("px", 12.5)
        assert lv["stop"] == ("px", 9.8)

    def test_percent_prices(self):
        rep = "- 目标价: +8%（第一目标）\n- 止损位: -5%（触发离场）"
        lv = sh.parse_price_levels(rep)
        assert lv["target"] == ("pct", 8.0)
        assert lv["stop"] == ("pct", -5.0)

    def test_missing(self):
        lv = sh.parse_price_levels("无价格信息")
        assert lv["target"] is None and lv["stop"] is None

    def test_first_number_wins(self):
        rep = "- 目标价: 12.5 / 13.2（第二目标）\n- 止损位: 9.8元"
        lv = sh.parse_price_levels(rep)
        assert lv["target"] == ("px", 12.5)


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

    def _rec_levels(self, direction="买入", stop=("px", 9.5), target=("px", 12.0),
                    entry=10.0):
        rec = _record(direction, entry)
        rec["levels"] = {"stop": stop, "target": target,
                         "raw_stop": "- 止损位: x", "raw_target": "- 目标价: y"}
        return rec

    def test_stop_hit_first(self, monkeypatch):
        bars = [("2026-07-02", 10.2, 10.3, 9.4, 9.6),  # 当天下影触及 9.5 止损
                ("2026-07-03", 9.6, 11.5, 9.6, 11.2)]
        _mock_ohlcv_hl(monkeypatch, bars)
        ev = sh.evaluate_call(self._rec_levels("买入"), asof_date="2026-07-06")
        assert ev["hit_first"] == "止损"
        assert ev["hit_bar"] == 1
        assert ev["hit_pct"] == pytest.approx(-5.0)
        assert ev["stop_px"] == 9.5 and ev["target_px"] == 12.0

    def test_target_hit_first(self, monkeypatch):
        bars = [("2026-07-02", 10.2, 12.1, 10.1, 11.8),  # 触及 12.0 目标
                ("2026-07-03", 11.8, 11.9, 10.0, 10.2)]
        _mock_ohlcv_hl(monkeypatch, bars)
        ev = sh.evaluate_call(self._rec_levels("买入"), asof_date="2026-07-06")
        assert ev["hit_first"] == "目标"
        assert ev["hit_bar"] == 1
        assert ev["hit_pct"] == pytest.approx(20.0)

    def test_same_bar_conflict_conservative(self, monkeypatch):
        bars = [("2026-07-02", 10.2, 12.1, 9.4, 11.0)]  # 同日上下插针双触发
        _mock_ohlcv_hl(monkeypatch, bars)
        ev = sh.evaluate_call(self._rec_levels("买入"), asof_date="2026-07-06")
        assert ev["hit_first"] == "止损"

    def test_sell_target_first(self, monkeypatch):
        rec = self._rec_levels("卖出", stop=("px", 10.8), target=("px", 9.2))
        bars = [("2026-07-02", 9.8, 9.9, 9.1, 9.3)]
        _mock_ohlcv_hl(monkeypatch, bars)
        ev = sh.evaluate_call(rec, asof_date="2026-07-06")
        assert ev["hit_first"] == "目标"
        assert ev["hit_bar"] == 1
        assert ev["hit_pct"] == pytest.approx(round((10 / 9.2 - 1) * 100, 2))

    def test_percent_levels_resolved(self, monkeypatch):
        rec = self._rec_levels("买入", stop=("pct", -5), target=("pct", 10))
        _mock_ohlcv_hl(monkeypatch, [("2026-07-02", 10.2, 10.3, 9.4, 9.6)])
        ev = sh.evaluate_call(rec, asof_date="2026-07-06")
        assert ev["stop_px"] == pytest.approx(9.5)
        assert ev["target_px"] == pytest.approx(11.0)
        assert ev["hit_first"] == "止损"

    def test_no_hit_within_window(self, monkeypatch):
        bars = [("2026-07-02", 10.2, 10.9, 10.0, 10.8),
                ("2026-07-03", 10.8, 11.9, 10.5, 11.7)]
        _mock_ohlcv_hl(monkeypatch, bars)
        ev = sh.evaluate_call(self._rec_levels("买入"), asof_date="2026-07-06")
        assert ev["hit_first"] is None
        assert ev["hit_pct"] is None

    def test_invalid_levels_ignored(self, monkeypatch):
        # 买入但止损价高于基准 → 不参与路径扫描
        rec = self._rec_levels("买入", stop=("px", 10.5), target=("px", 12.0))
        _mock_ohlcv_hl(monkeypatch, [("2026-07-02", 10.2, 10.3, 10.0, 10.1)])
        ev = sh.evaluate_call(rec, asof_date="2026-07-06")
        assert ev["stop_px"] is None
        assert ev["target_px"] == 12.0

    def test_levels_from_report_fallback(self, monkeypatch):
        rec = _record("买入")
        rec["report"] = ("### 交易计划\n- 目标价: 11.0\n- 止损位: 9.5")
        _mock_ohlcv_hl(monkeypatch, [("2026-07-02", 10.2, 11.2, 10.0, 11.0)])
        ev = sh.evaluate_call(rec, asof_date="2026-07-06")
        assert ev["hit_first"] == "目标"
        assert ev["hit_pct"] == pytest.approx(10.0)


class TestLoadPastEvaluations:
    def _setup(self, monkeypatch):
        summaries = [
            {"path": "/p/1.json", "kind": "stock", "ticker": "000725",
             "trade_date": "2026-07-05", "ts": 3},
            {"path": "/p/2.json", "kind": "stock", "ticker": "000725",
             "trade_date": "2026-07-01", "ts": 2},
            {"path": "/p/3.json", "kind": "stock", "ticker": "000725",
             "trade_date": "2026-06-20", "ts": 1},
        ]
        monkeypatch.setattr(sh, "list_records", lambda **kw: summaries)
        monkeypatch.setattr(sh, "load_record",
                            lambda p: {"ticker": "000725", "path": p,
                                       "parsed": {"direction": "买入", "confidence": "高"}})
        monkeypatch.setattr(sh, "evaluate_call",
                            lambda rec, asof_date=None: {"verdict": "对", "verdict_basis": "x",
                                                         "t3_close_pct": 5.0})
        return summaries

    def test_filters_and_caps(self, monkeypatch):
        self._setup(monkeypatch)
        out = sh.load_past_evaluations("000725", "2026-07-10", n=2)
        assert len(out) == 2
        assert out[0]["evaluation"]["verdict"] == "对"

    def test_excludes_same_day_and_future(self, monkeypatch):
        self._setup(monkeypatch)
        out = sh.load_past_evaluations("000725", "2026-07-01")
        assert len(out) == 1  # 只剩 06-20
        assert out[0]["record"]["path"] == "/p/3.json"

    def test_single_failure_skipped(self, monkeypatch):
        self._setup(monkeypatch)
        def boom(rec, asof_date=None):
            raise RuntimeError("ohlcv fail")
        monkeypatch.setattr(sh, "evaluate_call", boom)
        assert sh.load_past_evaluations("000725", "2026-07-10") == []


class TestHistoryBlock:
    def test_empty(self):
        from tradingagents.shortterm.prompts import history_block
        assert history_block([]) == ""

    def test_render(self):
        from tradingagents.shortterm.prompts import history_block
        past = [{
            "record": {"trade_date": "2026-06-30",
                       "parsed": {"direction": "买入", "confidence": "高"}},
            "evaluation": {"verdict": "错", "verdict_basis": "买入 vs T+3 收益 -3.46%",
                           "t1_close_pct": 1.04, "t3_close_pct": -3.46,
                           "t10_close_pct": -19.12},
        }]
        block = history_block(past)
        assert "2026-06-30" in block
        assert "方向=买入" in block
        assert "T+3 -3.46%" in block
        assert "T+10 -19.12%" in block
        assert "判定 错" in block
        assert "上次错在哪" in block


class TestAggregateStats:
    def _setup(self, monkeypatch):
        recs = [
            {"path": "/p/1.json", "kind": "stock", "ticker": "000725",
             "trade_date": "2026-07-01", "ts": 3},
            {"path": "/p/2.json", "kind": "stock", "ticker": "000725",
             "trade_date": "2026-06-20", "ts": 2},
            {"path": "/p/3.json", "kind": "stock", "ticker": "600000",
             "trade_date": "2026-06-15", "ts": 1},
            {"path": "/p/4.json", "kind": "stock", "ticker": "600000",
             "trade_date": "2026-06-10", "ts": 0},
            {"path": "/p/5.json", "kind": "screen", "trade_date": "2026-07-02", "ts": 4},
        ]
        full = {
            "/p/1.json": {"path": "/p/1.json", "ticker": "000725", "name": "京东方A",
                          "trade_date": "2026-07-01",
                          "parsed": {"direction": "买入", "confidence": "高"}},
            "/p/2.json": {"path": "/p/2.json", "ticker": "000725", "name": "京东方A",
                          "trade_date": "2026-06-20",
                          "parsed": {"direction": "买入", "confidence": "中"}},
            "/p/3.json": {"path": "/p/3.json", "ticker": "600000", "name": "浦发银行",
                          "trade_date": "2026-06-15",
                          "parsed": {"direction": "卖出", "confidence": "低"}},
            "/p/4.json": {"path": "/p/4.json", "ticker": "600000", "name": "浦发银行",
                          "trade_date": "2026-06-10",
                          "parsed": {"direction": "观望", "confidence": "低"}},
        }
        evals = {
            "/p/1.json": {"verdict": "对", "t1_close_pct": 2.0, "t3_close_pct": 5.0,
                          "t10_close_pct": 8.0},
            "/p/2.json": {"verdict": "错", "t1_close_pct": -1.0, "t3_close_pct": -3.0,
                          "t10_close_pct": None},
            "/p/3.json": {"verdict": "对", "t1_close_pct": 1.0, "t3_close_pct": 2.0,
                          "t10_close_pct": 4.0},
            "/p/4.json": {"verdict": "不评分", "t1_close_pct": None, "t3_close_pct": None,
                          "t10_close_pct": None},
        }
        monkeypatch.setattr(sh, "load_record", lambda p: full[p])
        monkeypatch.setattr(
            sh, "evaluate_call",
            lambda rec, asof_date=None, df=None: evals[rec["path"]],
        )
        import tradingagents.dataflows.a_stock as a
        monkeypatch.setattr(a, "_load_ohlcv_astock", lambda code, asof: _df_after([]))
        return recs

    def test_empty(self):
        s = sh.aggregate_stats([])
        assert s["total"] == 0 and s["scored"] == 0 and s["pending"] == 0
        assert s["win_rate"] is None

    def test_mixed(self, monkeypatch):
        recs = self._setup(monkeypatch)
        s = sh.aggregate_stats(recs)
        assert s["total"] == 4          # screen 记录不计
        assert s["scored"] == 3         # 观望 1 条不评分
        assert s["pending"] == 1
        assert s["wins"] == 2 and s["losses"] == 1
        assert s["win_rate"] == round(2 / 3 * 100, 1)
        b = s["by_direction"]["买入"]
        assert b["count"] == 2 and b["wins"] == 1 and b["losses"] == 1
        assert b["win_rate"] == 50.0
        assert b["avg_t3"] == 1.0       # (5 + -3) / 2
        s_ = s["by_direction"]["卖出"]
        assert s_["count"] == 1 and s_["wins"] == 1 and s_["win_rate"] == 100.0
        assert s["avg_t1_close"] == round((2.0 + -1.0 + 1.0) / 3, 2)  # 0.67
        assert s["avg_t3_close"] == round((5.0 + -3.0 + 2.0) / 3, 2)
        assert s["best"]["ticker"] == "000725" and s["best"]["t3"] == 5.0
        assert s["worst"]["t3"] == -3.0
        assert s["recent"][0]["trade_date"] == "2026-07-01"  # 新→旧

    def test_all_pending(self, monkeypatch):
        recs = self._setup(monkeypatch)
        monkeypatch.setattr(
            sh, "evaluate_call",
            lambda rec, asof_date=None, df=None: {"verdict": "待验证",
                                                  "t3_close_pct": None,
                                                  "t1_close_pct": None,
                                                  "t10_close_pct": None},
        )
        s = sh.aggregate_stats(recs)
        assert s["scored"] == 0 and s["pending"] == 4
        assert s["win_rate"] is None and s["best"] is None
