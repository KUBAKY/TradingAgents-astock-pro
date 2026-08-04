"""analysis_registry 注册表测试（无网络/无 LLM，全部走 env 临时目录）。"""

import json

import pytest

from tradingagents.analysis_registry import registry
from tradingagents.analysis_registry.registry import (
    backfill_index,
    direction_from_rating,
    get_stock_timeline,
    query,
    record_id,
    register,
    register_deep_review_record,
    register_mainline_record,
    register_screen_record,
    register_stock_record,
    registry_dir,
    update_record,
)


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    shortterm = tmp_path / "shortterm"
    logs = tmp_path / "logs"
    deepreview = tmp_path / "deepreview"
    reg = tmp_path / "registry"
    for d in (shortterm, logs, deepreview, reg):
        d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("TRADINGAGENTS_REGISTRY_DIR", str(reg))
    monkeypatch.setenv("TRADINGAGENTS_SHORTTERM_DIR", str(shortterm))
    monkeypatch.setenv("TRADINGAGENTS_LOGS_DIR", str(logs))
    monkeypatch.setenv("TRADINGAGENTS_DEEPREVIEW_DIR", str(deepreview))
    return {"shortterm": shortterm, "logs": logs, "deepreview": deepreview,
            "registry": reg}


def _stock_file(ticker="000725", name="京东方A", trade_date="2026-07-31",
                ts=100, inputs=None, report="", ch0=None):
    return {
        "kind": "stock", "ticker": ticker, "name": name,
        "trade_date": trade_date, "ts": ts, "mode": "ai",
        "inputs": inputs or {"intent": "test"},
        "ch0": ch0 or {"ticker": ticker, "trade_date": trade_date,
                       "metrics": {"last_close": 4.0},
                       "anomalies": [{"type": "limit_up", "signal": "涨停"}]},
        "report": report or "**方向**：买入\n**置信度**：高\n",
        "parsed": {"direction": "买入", "confidence": "高", "horizon": "3-10日波段"},
        "levels": {"target": ("px", 4.5), "stop": ("px", 3.8),
                   "raw_target": "-目标价 4.5", "raw_stop": "-止损位 3.8"},
    }


def _screen_file(trade_date="2026-08-01", ts=200, report=None):
    return {
        "kind": "screen", "trade_date": trade_date, "ts": ts,
        "capital": 100000,
        "scan": {"trade_date": trade_date,
                 "market_sentiment": {"label": "升温", "limit_up_count": 60,
                                      "max_streak": 5}},
        "report": report or "TOP1 300750 理由a\nTOP2 000725 理由b",
    }


def _deepreview_file(ticker="300750", trade_date="2026-08-02", ts=300.5,
                     signal="Buy", reason="持仓异常复核"):
    return {
        "kind": "deep_review", "ticker": ticker, "trade_date": trade_date,
        "reason": reason, "ts": ts, "signal": signal,
        "report": "正文", "analyst_reports": {},
    }


def _mainline_file(logs_dir, ticker="000725", trade_date="2026-07-31",
                   decision="最终评级：强力买入"):
    d = logs_dir / ticker / "TradingAgentsStrategy_logs"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"full_states_log_{trade_date}.json"
    p.write_text(json.dumps({"final_trade_decision": decision,
                             "market_report": "m"}), encoding="utf-8")
    return p


class TestRegister:
    def test_writes_index_and_returns_id(self, dirs):
        rid = register("stock", "000725", "2026-07-31", ts=100,
                       path=str(dirs["shortterm"] / "000725_2026-07-31_100.json"),
                       name="京东方A", summary={"direction": "买入"})
        assert rid == record_id("stock", "000725", "2026-07-31", 100)
        idx = json.loads((registry_dir() / "index.json").read_text(encoding="utf-8"))
        assert len(idx["records"]) == 1
        assert idx["records"][0]["summary"] == {"direction": "买入"}

    def test_idempotent_same_id_no_dup(self, dirs):
        kw = dict(kind="stock", ticker="000725", trade_date="2026-07-31",
                  ts=100, path="/x.json")
        r1 = register(**kw)
        r2 = register(**kw)
        assert r1 == r2
        assert len(query()) == 1

    def test_ts_float_coerced_to_int(self, dirs):
        register("deep_review", "300750", "2026-08-02", ts=300.5, path="/d.json")
        r = query(kind="deep_review")[0]
        assert r["ts"] == 300
        assert r["id"] == record_id("deep_review", "300750", "2026-08-02", 300)

    def test_missing_fields_returns_none(self, dirs):
        assert register("stock", "", "2026-07-31", path="/x.json") is None
        assert register("stock", "000725", "", path="/x.json") is None
        assert register("stock", "000725", "2026-07-31", path="") is None
        assert register("bogus", "000725", "2026-07-31", path="/x.json") is None
        assert len(query()) == 0


class TestQuery:
    def _seed(self, dirs):
        for ts, d in ((100, "2026-07-31"), (150, "2026-08-03")):
            p = dirs["shortterm"] / f"000725_{d}_{ts}.json"
            p.write_text(json.dumps(_stock_file(trade_date=d, ts=ts)),
                         encoding="utf-8")
        p2 = dirs["shortterm"] / "300750_2026-08-01_120.json"
        p2.write_text(json.dumps(_stock_file(ticker="300750", name="宁德时代",
                                             trade_date="2026-08-01", ts=120)),
                      encoding="utf-8")

    def test_ticker_and_kind_filters(self, dirs):
        self._seed(dirs)
        assert len(query(ticker="000725")) == 2
        assert len(query(ticker="000725", kind="stock")) == 2
        assert len(query(ticker="300750")) == 1
        assert len(query(kind="mainline")) == 0
        assert len(query(ticker="999999")) == 0

    def test_since_and_limit_and_order(self, dirs):
        self._seed(dirs)
        rs = query(kind="stock")
        assert [r["ts"] for r in rs] == [150, 120, 100]
        assert len(query(kind="stock", since=110)) == 2
        assert len(query(kind="stock", limit=1)) == 1
        assert query(kind="stock", limit=1)[0]["ts"] == 150

    def test_update_record(self, dirs):
        rid = register("stock", "000725", "2026-07-31", ts=100, path="/x.json")
        assert update_record(rid, validation={"verdict": "对"}) is True
        assert query(ticker="000725")[0]["validation"] == {"verdict": "对"}
        assert update_record("nope", validation={}) is False


class TestTimeline:
    def test_ascending_cross_kind(self, dirs):
        p1 = dirs["shortterm"] / "000725_2026-07-31_100.json"
        p1.write_text(json.dumps(_stock_file(ts=100)), encoding="utf-8")
        p2 = dirs["deepreview"] / "000725_2026-08-02_300500.json"
        p2.write_text(json.dumps(_deepreview_file(ticker="000725",
                                                  trade_date="2026-08-02")),
                      encoding="utf-8")
        _mainline_file(dirs["logs"], ticker="000725", trade_date="2026-08-04")
        tl = get_stock_timeline("000725")
        assert [r["kind"] for r in tl] == ["stock", "deep_review", "mainline"]
        assert all(tl[i]["ts"] <= tl[i + 1]["ts"] for i in range(len(tl) - 1))


class TestBackfill:
    def _seed_all(self, dirs):
        (dirs["shortterm"] / "000725_2026-07-31_100.json").write_text(
            json.dumps(_stock_file(ts=100)), encoding="utf-8")
        (dirs["shortterm"] / "600519_2026-08-01_110.json").write_text(
            json.dumps(_stock_file(ticker="600519", name="贵州茅台",
                                   trade_date="2026-08-01", ts=110,
                                   inputs={"kind": "follow",
                                           "trade_date": "2026-08-01"})),
            encoding="utf-8")
        (dirs["shortterm"] / "screener_2026-08-01_200.json").write_text(
            json.dumps(_screen_file()), encoding="utf-8")
        (dirs["shortterm"] / "broken.json").write_text("{not json", encoding="utf-8")
        (dirs["deepreview"] / "300750_2026-08-02_300500.json").write_text(
            json.dumps(_deepreview_file()), encoding="utf-8")
        _mainline_file(dirs["logs"])

    def test_backfill_all_sources(self, dirs):
        self._seed_all(dirs)
        counts = backfill_index()
        assert counts["stock_follow"] == 5  # 2 stock/follow + 1 screen + 2 picks
        assert counts["deep_review"] == 1
        assert counts["mainline"] == 1
        rs = query()
        assert len(rs) == counts["total"] == 7
        kinds = {r["kind"] for r in rs}
        assert kinds == {"stock", "follow", "screen", "pick", "deep_review",
                         "mainline"}

    def test_backfill_summaries(self, dirs):
        self._seed_all(dirs)
        backfill_index()
        stock = next(r for r in query() if r["kind"] == "stock")
        assert stock["summary"]["direction"] == "买入"
        assert stock["summary"]["last_close"] == 4.0
        assert stock["summary"]["anomaly_types"] == ["limit_up"]
        follow = next(r for r in query() if r["kind"] == "follow")
        assert follow["ticker"] == "600519"
        screen = next(r for r in query() if r["kind"] == "screen")
        assert screen["summary"]["sentiment"] == "升温"
        assert screen["ticker"] == ""
        picks = [r for r in query() if r["kind"] == "pick"]
        assert {r["ticker"] for r in picks} == {"300750", "000725"}
        assert sorted(r["summary"]["rank"] for r in picks) == [1, 2]
        dr = next(r for r in query() if r["kind"] == "deep_review")
        assert dr["summary"]["rating"] == "Buy"
        assert dr["summary"]["direction"] == "买入"
        ml = next(r for r in query() if r["kind"] == "mainline")
        assert ml["summary"]["rating"] == "Buy"
        assert ml["summary"]["direction"] == "买入"

    def test_backfill_idempotent(self, dirs):
        self._seed_all(dirs)
        c1 = backfill_index()
        c2 = backfill_index()
        assert c2["total"] == c1["total"] == 7
        assert c2["stock_follow"] == 0
        assert c2["deep_review"] == 0
        assert c2["mainline"] == 0

    def test_query_triggers_lazy_backfill(self, dirs):
        self._seed_all(dirs)
        assert len(query()) == 7

    def test_register_after_backfill_no_dup(self, dirs):
        self._seed_all(dirs)
        backfill_index()
        p = dirs["shortterm"] / "000725_2026-07-31_100.json"
        rid = register("stock", "000725", "2026-07-31", ts=100, path=str(p))
        assert len(query(ticker="000725", kind="stock")) == 1
        assert query(ticker="000725", kind="stock")[0]["id"] == rid


class TestHelpers:
    def test_direction_from_rating(self):
        assert direction_from_rating("Buy") == "买入"
        assert direction_from_rating("Overweight") == "买入"
        assert direction_from_rating("Hold") == "观望"
        assert direction_from_rating("Underweight") == "卖出"
        assert direction_from_rating("Sell") == "卖出"
        assert direction_from_rating(None) is None
        assert direction_from_rating("N/A") is None

    def test_record_id_format(self):
        assert record_id("stock", "000725", "2026-07-31", 100) == \
            "stock:000725:2026-07-31:100"


class TestRegisterWrappers:
    def test_stock_record_kind_and_summary(self, dirs):
        p = dirs["shortterm"] / "000725_2026-07-31_100.json"
        rid = register_stock_record(_stock_file(ts=100), str(p))
        r = query(ticker="000725", kind="stock")[0]
        assert r["id"] == rid
        assert r["summary"]["direction"] == "买入"
        assert r["summary"]["last_close"] == 4.0

    def test_follow_record_kind(self, dirs):
        p = dirs["shortterm"] / "600519_2026-08-01_110.json"
        rec = _stock_file(ticker="600519", ts=110,
                          inputs={"kind": "follow", "trade_date": "2026-08-01"})
        register_stock_record(rec, str(p))
        r = query(ticker="600519")[0]
        assert r["kind"] == "follow"

    def test_screen_record_with_picks(self, dirs):
        p = dirs["shortterm"] / "screener_2026-08-01_200.json"
        register_screen_record(_screen_file(), str(p))
        assert len(query(kind="screen")) == 1
        picks = query(kind="pick")
        assert {r["ticker"] for r in picks} == {"300750", "000725"}

    def test_deep_review_record(self, dirs):
        p = dirs["deepreview"] / "300750_2026-08-02_300500.json"
        register_deep_review_record(_deepreview_file(), str(p))
        r = query(kind="deep_review")[0]
        assert r["summary"]["rating"] == "Buy"
        assert r["summary"]["direction"] == "买入"
        assert r["summary"]["reason"] == "持仓异常复核"

    def test_mainline_record_from_state(self, dirs):
        p = _mainline_file(dirs["logs"], decision="最终评级：强力买入")
        register_mainline_record("000725", "2026-07-31", str(p),
                                 {"final_trade_decision": "最终评级：强力买入"})
        r = query(kind="mainline")[0]
        assert r["summary"]["rating"] == "Buy"
        assert r["summary"]["direction"] == "买入"
        assert r["ts"] == int(p.stat().st_mtime)

    def test_mainline_missing_file_uses_now(self, dirs):
        register_mainline_record("000725", "2026-07-31", "/no/such/file.json", {})
        r = query(kind="mainline")[0]
        assert r["ts"] > 0
        assert r["summary"]["rating"] == "N/A"
