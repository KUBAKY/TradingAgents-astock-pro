"""validate.py 统一验证引擎测试。conftest 已隔离 registry/数据源目录。"""

from __future__ import annotations

import json

import pandas as pd
import pytest

from tradingagents.analysis_registry import registry
from tradingagents.analysis_registry.validate import (
    load_validation,
    plan_validation_window,
    run_validations,
    validate_record,
)


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def _df(rec_date="2026-07-01", after=None, rec_close=10.0):
    """含记录日行的 K 线 DataFrame。after: [(date, open, close), ...]"""
    rows = [(rec_date, rec_close, rec_close)] + (after or [])
    return pd.DataFrame({
        "Date": pd.to_datetime([r[0] for r in rows]),
        "Open": [r[1] for r in rows],
        "Close": [r[2] for r in rows],
        "High": [max(r[1], r[2]) for r in rows],
        "Low": [min(r[1], r[2]) for r in rows],
        "Volume": [1000.0] * len(rows),
    })


def _mock_ohlcv(monkeypatch, df):
    import tradingagents.dataflows.a_stock as a
    monkeypatch.setattr(a, "_load_ohlcv_astock", lambda code, asof: df)


def _rec(rid="stock:000725:2026-07-01:100", kind="stock", ticker="000725",
         trade_date="2026-07-01", path="/x/a.json", **summary):
    return {
        "id": rid, "kind": kind, "ticker": ticker, "name": "",
        "trade_date": trade_date, "ts": 100, "path": path,
        "summary": summary, "validation": None,
    }


def _write(path, payload):
    from pathlib import Path
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


_RISE = [("2026-07-02", 10.0, 10.4), ("2026-07-03", 10.3, 10.5),
         ("2026-07-06", 10.4, 10.6)]
_FALL = [("2026-07-02", 10.0, 9.6), ("2026-07-03", 9.7, 9.4),
         ("2026-07-06", 9.5, 9.2)]


class TestWindows:
    def test_shortterm_3(self):
        assert plan_validation_window({"kind": "stock"}) == 3
        assert plan_validation_window({"kind": "follow"}) == 3

    def test_others_10(self):
        for k in ("screen", "pick", "deep_review", "mainline"):
            assert plan_validation_window({"kind": k}) == 10

    def test_unknown_default(self):
        assert plan_validation_window({"kind": "nope"}) == 10


class TestShortTerm:
    @pytest.fixture
    def stock_rec(self, tmp_path):
        p = tmp_path / "a.json"
        _write(p, {
            "kind": "stock", "ticker": "000725", "trade_date": "2026-07-01",
            "ch0": {"metrics": {"last_close": 10.0}},
            "parsed": {"direction": "买入", "confidence": "高"},
            "levels": {},
        })
        return _rec(path=str(p))

    def test_buy_profit_ok(self, monkeypatch, stock_rec):
        _mock_ohlcv(monkeypatch, _df(after=_RISE))
        out = validate_record(stock_rec, asof="2026-08-04")
        assert out["verdict"] == "对"
        assert out["t3_close_pct"] > 0
        assert out["bars_after"] == 3

    def test_buy_loss_wrong(self, monkeypatch, stock_rec):
        _mock_ohlcv(monkeypatch, _df(after=_FALL))
        out = validate_record(stock_rec, asof="2026-08-04")
        assert out["verdict"] == "错"

    def test_sell_profit_wrong(self, monkeypatch, tmp_path):
        p = tmp_path / "s.json"
        _write(p, {
            "kind": "follow", "ticker": "600519", "trade_date": "2026-07-01",
            "ch0": {"metrics": {"last_close": 10.0}},
            "parsed": {"direction": "卖出"},
        })
        rec = _rec(rid="follow:600519:2026-07-01:1", kind="follow",
                   ticker="600519", path=str(p))
        _mock_ohlcv(monkeypatch, _df(after=_RISE))
        out = validate_record(rec, asof="2026-08-04")
        assert out["verdict"] == "错"

    def test_hold_not_scored(self, monkeypatch, stock_rec):
        _write(stock_rec["path"], {
            "kind": "stock", "ticker": "000725", "trade_date": "2026-07-01",
            "ch0": {"metrics": {"last_close": 10.0}},
            "parsed": {"direction": "观望"},
        })
        _mock_ohlcv(monkeypatch, _df(after=_RISE))
        out = validate_record(stock_rec, asof="2026-08-04")
        assert out["verdict"] == "不评分"

    def test_entry_close_fallback(self, monkeypatch, tmp_path):
        """ch0 缺基准收盘价 → 从 K 线补记录日收盘。"""
        p = tmp_path / "f.json"
        _write(p, {
            "kind": "stock", "ticker": "000725", "trade_date": "2026-07-01",
            "parsed": {"direction": "买入"},
        })
        rec = _rec(path=str(p))
        _mock_ohlcv(monkeypatch, _df(rec_close=10.0, after=_RISE))
        out = validate_record(rec, asof="2026-08-04")
        assert out["verdict"] == "对"
        assert out["entry_close"] == 10.0

    def test_no_bars_pending(self, monkeypatch, stock_rec):
        _mock_ohlcv(monkeypatch, _df(after=[]))
        out = validate_record(stock_rec, asof="2026-08-04")
        assert out["verdict"] == "待验证"

    def test_idempotent_skip_and_force(self, monkeypatch, stock_rec, capsys):
        _mock_ohlcv(monkeypatch, _df(after=_RISE))
        import tradingagents.shortterm.history as sh

        calls = {"n": 0}
        real = sh.evaluate_call

        def spy(*args, **kwargs):
            calls["n"] += 1
            return real(*args, **kwargs)
        monkeypatch.setattr(sh, "evaluate_call", spy)

        out1 = validate_record(stock_rec, asof="2026-08-04")
        assert calls["n"] == 1
        out2 = validate_record(stock_rec, asof="2026-08-04")
        assert calls["n"] == 1  # 幂等：不重算
        assert out2["verdict"] == out1["verdict"]
        out3 = validate_record(stock_rec, asof="2026-08-04", force=True)
        assert calls["n"] == 2  # force 重算

    def test_pending_retries(self, monkeypatch, stock_rec):
        """第一次 K 线不足待验证 → 后续 asof 重算。"""
        import tradingagents.dataflows.a_stock as a
        df = {"cur": _df(after=[])}

        def loader(code, asof):
            return df["cur"]
        monkeypatch.setattr(a, "_load_ohlcv_astock", loader)

        out1 = validate_record(stock_rec, asof="2026-07-02")
        assert out1["verdict"] == "待验证"
        df["cur"] = _df(after=_RISE)
        out2 = validate_record(stock_rec, asof="2026-08-04")
        assert out2["verdict"] == "对"

    def test_missing_file_degrades(self, monkeypatch, tmp_path):
        rec = _rec(path=str(tmp_path / "nope.json"))
        _mock_ohlcv(monkeypatch, _df(after=_RISE))
        out = validate_record(rec, asof="2026-08-04")
        assert out["verdict"] in ("待验证", "不评分", "验证失败")


class TestPick:
    def test_no_direction_not_scored(self, monkeypatch, tmp_path):
        p = tmp_path / "pick.json"
        _write(p, {"report": "TOP 3: 000725, 600519, 000001"})
        rec = _rec(rid="pick:000725:2026-07-01:5", kind="pick",
                   ticker="000725", path=str(p))
        _mock_ohlcv(monkeypatch, _df(after=_RISE))
        out = validate_record(rec, asof="2026-08-04")
        assert out["verdict"] == "不评分"
        assert out["t3_close_pct"] is not None


class TestDeepReview:
    def test_signal_buy(self, monkeypatch, tmp_path):
        p = tmp_path / "dr.json"
        _write(p, {
            "kind": "deep_review", "ticker": "000725", "trade_date": "2026-07-01",
            "signal": "Buy",
            "report": "Rating: **Buy**\n- 目标价: 12.5\n- 止损位: 9.8",
        })
        rec = _rec(rid="deep_review:000725:2026-07-01:9", kind="deep_review",
                   ticker="000725", path=str(p))
        _mock_ohlcv(monkeypatch, _df(after=_RISE))
        out = validate_record(rec, asof="2026-08-04")
        assert out["verdict"] == "对"
        assert out["raw_target"] != ""

    def test_signal_sell_fall(self, monkeypatch, tmp_path):
        p = tmp_path / "dr2.json"
        _write(p, {
            "kind": "deep_review", "ticker": "000725", "trade_date": "2026-07-01",
            "signal": "Sell", "report": "Rating: Sell",
        })
        rec = _rec(rid="deep_review:000725:2026-07-01:10", kind="deep_review",
                   ticker="000725", path=str(p))
        _mock_ohlcv(monkeypatch, _df(after=_FALL))
        out = validate_record(rec, asof="2026-08-04")
        assert out["verdict"] == "对"


class TestMainline:
    def test_rating_sell(self, monkeypatch, tmp_path):
        p = tmp_path / "ml.json"
        _write(p, {
            "final_trade_decision": "Rating: **Sell**\n- 目标价: 9.0\n- 止损位: 10.5",
        })
        rec = _rec(rid="mainline:600519:2026-07-01:11", kind="mainline",
                   ticker="600519", path=str(p))
        _mock_ohlcv(monkeypatch, _df(after=_FALL))
        out = validate_record(rec, asof="2026-08-04")
        assert out["verdict"] == "对"
        assert out["raw_stop"] != ""

    def test_no_rating_not_scored(self, monkeypatch, tmp_path):
        p = tmp_path / "ml2.json"
        _write(p, {"investment_plan": "无评级文本"})
        rec = _rec(rid="mainline:600519:2026-07-01:12", kind="mainline",
                   ticker="600519", path=str(p))
        _mock_ohlcv(monkeypatch, _df(after=_RISE))
        out = validate_record(rec, asof="2026-08-04")
        assert out["verdict"] == "不评分"


class TestScreen:
    def test_picks_evaluated(self, monkeypatch, tmp_path):
        p = tmp_path / "screen.json"
        _write(p, {"report": "TOP 3:\n1. 000725\n2. 600519"})
        rec = _rec(rid="screen::2026-07-01:7", kind="screen", ticker="",
                   path=str(p))
        _mock_ohlcv(monkeypatch, _df(after=_RISE))
        out = validate_record(rec, asof="2026-08-04")
        assert out["verdict"] == "多票汇总"
        assert len(out["picks"]) == 2
        assert out["picks"][0]["ticker"] == "000725"
        assert out["picks"][0]["t3_close_pct"] is not None


class TestPersistence:
    def test_files_and_index_sync(self, monkeypatch, tmp_path):
        p = tmp_path / "a.json"
        _write(p, {
            "kind": "stock", "ticker": "000725", "trade_date": "2026-07-01",
            "ch0": {"metrics": {"last_close": 10.0}},
            "parsed": {"direction": "买入"},
        })
        rid = registry.register("stock", "000725", "2026-07-01", ts=200,
                                path=str(p), summary={"direction": "买入"})
        assert rid
        rec = registry.query(ticker="000725")[0]
        _mock_ohlcv(monkeypatch, _df(after=_RISE))
        out = validate_record(rec, asof="2026-08-04")

        vp = registry.registry_dir() / "validate" / (
            rec["id"].replace(":", "_") + ".json")
        assert vp.exists()
        assert json.loads(vp.read_text(encoding="utf-8"))["verdict"] == out["verdict"]

        idx = registry.query()
        assert idx[0]["validation"]["verdict"] == out["verdict"]
        assert load_validation(rec["id"])["verdict"] == out["verdict"]


class TestRunValidations:
    def test_filter_and_degrade(self, monkeypatch, tmp_path):
        rid = registry.register(
            "stock", "000725", "2026-07-01", ts=300, path=str(tmp_path / "missing.json"),
            summary={"direction": "买入"},
        )
        assert rid
        _mock_ohlcv(monkeypatch, _df(after=_RISE))
        out = run_validations(ticker="000725", asof="2026-08-04")
        assert len(out) == 1  # 原文缺失 → 降级，不中断
        assert out[0]["record_id"] == rid

    def test_no_records(self):
        assert run_validations(ticker="999999") == []
