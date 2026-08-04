"""持仓管理：CRUD / 每日跟进（幂等）/ 盈亏快照。"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from tradingagents.shortterm import portfolio


@pytest.fixture
def port_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADINGAGENTS_PORTFOLIO_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def fresh(port_dir):
    for p in port_dir.rglob("*.json"):
        p.unlink()
    return port_dir


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    """禁 mootdx 名称映射 + 真实行情（测试不碰网络）。"""
    monkeypatch.setattr(portfolio, "_lookup_name", lambda t: "")
    def _no_quote(t, d):
        raise RuntimeError("offline")
    monkeypatch.setattr(portfolio, "run_ch0", _no_quote)


class TestCRUD:
    def test_add_list_remove(self, fresh):
        portfolio.add_position("000725", 4.2, 10000, "2026-05-10", "中长线")
        portfolio.add_position("600519", 1500.0, 100, note="压舱石")
        pos = portfolio.list_positions()
        assert len(pos) == 2
        assert pos[0]["ticker"] == "000725"
        assert pos[0]["cost_price"] == 4.2
        assert pos[0]["shares"] == 10000
        assert pos[0]["buy_date"] == "2026-05-10"
        assert pos[0]["note"] == "中长线"
        assert pos[1]["buy_date"] == date.today().isoformat()  # 缺省今日

        assert portfolio.remove_position("000725") is True
        assert portfolio.remove_position("000725") is False  # 已删
        assert [p["ticker"] for p in portfolio.list_positions()] == ["600519"]

    def test_add_validates(self, fresh):
        portfolio.add_position("000725", 4.2, 1000)
        with pytest.raises(ValueError):
            portfolio.add_position("000725", 4.2, 1000)  # 重复代码
        with pytest.raises(ValueError):
            portfolio.add_position("600000", 0, 100)     # 成本 <= 0
        with pytest.raises(ValueError):
            portfolio.add_position("600000", 4.2, -5)    # 股数 <= 0
        with pytest.raises(ValueError):
            portfolio.add_position("not_a_ticker_zz", 4.2, 100)  # 非法代码

    def test_add_resolves_name(self, fresh, monkeypatch):
        monkeypatch.setattr(portfolio, "_lookup_name", lambda t: "京东方A")
        portfolio.add_position("000725", 4.2, 1000)
        assert portfolio.list_positions()[0]["name"] == "京东方A"

    def test_add_name_lookup_failure_keeps_empty(self, fresh, monkeypatch):
        def boom(t):
            raise RuntimeError("mootdx down")
        monkeypatch.setattr(portfolio, "_lookup_name", boom)
        portfolio.add_position("000725", 4.2, 1000)
        assert portfolio.list_positions()[0]["name"] == ""

    def test_update_position(self, fresh):
        portfolio.add_position("000725", 4.2, 1000)
        portfolio.update_position("000725", cost_price=4.5, note="加仓")
        pos = portfolio.list_positions()[0]
        assert pos["cost_price"] == 4.5
        assert pos["shares"] == 1000       # 未改字段保留
        assert pos["note"] == "加仓"
        with pytest.raises(ValueError):
            portfolio.update_position("600000", cost_price=1.0)  # 不存在

    def test_corrupt_state_recovers_empty(self, fresh):
        (fresh / "state.json").write_text("{broken json", encoding="utf-8")
        assert portfolio.list_positions() == []
        portfolio.add_position("000725", 4.2, 1000)
        assert len(portfolio.list_positions()) == 1  # 覆盖损坏文件


class TestSnapshot:
    def test_snapshot_pnl(self, fresh, monkeypatch):
        monkeypatch.setattr(portfolio, "run_ch0", lambda t, d: {
            "ticker": t, "name": "京东方A", "trade_date": d,
            "metrics": {"last_close": 5.0},
        })
        portfolio.add_position("000725", 4.0, 100, buy_date="2026-05-10")
        portfolio.take_snapshot("2026-08-03")
        snap = portfolio.load_snapshot("2026-08-03")
        row = snap["positions"][0]
        assert row["last_close"] == 5.0
        assert row["market_value"] == 500.0
        assert row["pnl"] == 100.0
        assert row["pnl_pct"] == pytest.approx(25.0)
        assert snap["total_pnl"] == 100.0
        assert snap["total_value"] == 500.0

    def test_snapshot_handles_missing_quote(self, fresh, monkeypatch):
        def _fail(t, d):
            raise RuntimeError("无行情")
        monkeypatch.setattr(portfolio, "run_ch0", _fail)
        portfolio.add_position("000725", 4.0, 100)
        portfolio.take_snapshot("2026-08-03")
        snap = portfolio.load_snapshot("2026-08-03")
        row = snap["positions"][0]
        assert row["last_close"] is None
        assert row["pnl"] is None
        assert row["error"] == "无行情"

    def test_snapshot_saved_and_loaded(self, fresh, monkeypatch):
        monkeypatch.setattr(portfolio, "run_ch0", lambda t, d: {
            "ticker": t, "name": "京东方A", "trade_date": d,
            "metrics": {"last_close": 5.0},
        })
        portfolio.add_position("000725", 4.0, 100)
        path = portfolio.take_snapshot("2026-08-03")
        assert path.name == "2026-08-03.json"
        loaded = portfolio.load_snapshot("2026-08-03")
        assert loaded["total_pnl"] == 100.0
        assert loaded["pnl_pct"] == pytest.approx(25.0)
        assert loaded["total_cost"] == pytest.approx(400.0)
        assert portfolio.list_snapshots() == ["2026-08-03"]


class TestDailyFollow:
    def _fake_run(self, ticker, trade_date, **kw):
        return {"ch0": {"ticker": ticker, "name": "京东方A", "trade_date": trade_date,
                        "metrics": {"last_close": 5.0}},
                "mode": "swing",
                "report": f"**方向**：买入\n\n**置信度**：高\n\n**适用周期**：3-10日波段",
                "bundle": "", "validation": {"ok": True, "violations": [], "retried": False},
                "cost": {"calls": 1, "total_cost_cny": 0.1}}

    def test_follow_runs_once_idempotent(self, fresh, monkeypatch):
        calls = {"n": 0}
        def fake_run(ticker, trade_date, **kw):
            calls["n"] += 1
            return self._fake_run(ticker, trade_date)
        monkeypatch.setattr(portfolio, "pipeline_run", fake_run)
        monkeypatch.setattr(portfolio, "save_stock_record", lambda r, i: None)
        portfolio.add_position("000725", 4.0, 100, buy_date="2026-05-10")

        r1 = portfolio.run_daily_follow("2026-08-03")
        assert r1["skipped"] is False
        assert calls["n"] == 1
        assert r1["results"][0]["direction"] == "买入"
        assert r1["results"][0]["cost_cny"] == 0.1

        r2 = portfolio.run_daily_follow("2026-08-03")  # 幂等：已有快照不重跑
        assert r2["skipped"] is True
        assert calls["n"] == 1

        r3 = portfolio.run_daily_follow("2026-08-03", force=True)  # 强制重跑
        assert r3["skipped"] is False
        assert calls["n"] == 2

    def test_follow_passes_position_and_feature(self, fresh, monkeypatch):
        seen = {}
        def fake_run(ticker, trade_date, **kw):
            seen.update(kw)
            return self._fake_run(ticker, trade_date)
        monkeypatch.setattr(portfolio, "pipeline_run", fake_run)
        monkeypatch.setattr(portfolio, "save_stock_record", lambda r, i: None)
        portfolio.add_position("000725", 4.0, 100, buy_date="2026-05-10")
        portfolio.run_daily_follow("2026-08-03")
        assert seen["cost"] == 4.0
        assert seen["shares"] == 100
        assert seen["cost_feature"] == "portfolio"
        assert seen["trace"] is False

    def test_follow_continues_on_failure(self, fresh, monkeypatch):
        def fake_run(ticker, trade_date, **kw):
            if ticker == "000725":
                raise RuntimeError("LLM 挂了")
            return self._fake_run(ticker, trade_date)
        monkeypatch.setattr(portfolio, "pipeline_run", fake_run)
        monkeypatch.setattr(portfolio, "save_stock_record", lambda r, i: None)
        portfolio.add_position("000725", 4.0, 100)
        portfolio.add_position("600519", 1500.0, 100)
        r = portfolio.run_daily_follow("2026-08-03")
        assert r["results"][0]["error"] == "LLM 挂了"
        assert r["results"][1]["direction"] == "买入"  # 第二只照常
        assert r["failed"] == 1

    def test_follow_saves_records(self, fresh, monkeypatch):
        saved = []
        def fake_save(result, inputs):
            saved.append((result, inputs))
            return None
        monkeypatch.setattr(portfolio, "pipeline_run", self._fake_run)
        monkeypatch.setattr(portfolio, "save_stock_record", fake_save)
        portfolio.add_position("000725", 4.0, 100)
        portfolio.run_daily_follow("2026-08-03")
        assert len(saved) == 1
        assert saved[0][1]["kind"] == "follow"
