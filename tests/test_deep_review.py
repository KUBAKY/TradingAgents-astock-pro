"""共通件 deep_review + #4 extract_picks 测试（无网络/无 LLM）。"""

import json

import pytest

from tradingagents.shortterm import deep_review
from tradingagents.shortterm.screener import extract_picks


@pytest.fixture
def dr_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADINGAGENTS_DEEPREVIEW_DIR", str(tmp_path))
    return tmp_path


class _FakeGraph:
    def __init__(self, *a, **k):
        pass

    def propagate(self, ticker, trade_date):
        return ({
            "final_trade_decision": "**评级**: 强烈看多\n正文",
            "market_report": "m", "sentiment_report": "s",
            "news_report": "n", "fundamentals_report": "f",
            "policy_report": "p", "hot_money_report": "h",
            "lockup_report": "l", "data_quality_summary": "d",
            "investment_plan": "i", "trader_investment_plan": "t",
        }, "Buy")


class TestRunDeepReview:
    def test_success_persists_and_returns(self, dr_dir, monkeypatch):
        monkeypatch.setattr("tradingagents.graph.trading_graph.TradingAgentsGraph",
                            _FakeGraph)
        out = deep_review.run_deep_review("300750", "2026-08-04",
                                          reason="扫描TOP N深度辩论")
        assert out["ok"] is True
        assert out["signal"] == "Buy"
        assert out["ticker"] == "300750"
        assert "强烈看多" in out["report"]
        path = out["report_path"]
        assert path.endswith(".json")
        rec = json.loads(deep_review.deep_review_dir().joinpath(path.split("/")[-1]).read_text(encoding="utf-8"))
        assert rec["kind"] == "deep_review"
        assert rec["reason"] == "扫描TOP N深度辩论"
        assert rec["signal"] == "Buy"
        assert rec["analyst_reports"]["policy_report"] == "p"

    def test_failure_returns_error_no_raise(self, dr_dir, monkeypatch):
        class Boom:
            def __init__(self, *a, **k):
                pass

            def propagate(self, *a, **k):
                raise RuntimeError("graph crashed")
        monkeypatch.setattr("tradingagents.graph.trading_graph.TradingAgentsGraph", Boom)
        out = deep_review.run_deep_review("000725", "2026-08-04", reason="x")
        assert out["ok"] is False
        assert "graph crashed" in out["error"]
        assert not list(deep_review.deep_review_dir().glob("*.json"))

    def test_import_failure_returns_error(self, dr_dir, monkeypatch):
        def boom(*a, **k):
            raise ImportError("no module")
        monkeypatch.setattr("tradingagents.graph.trading_graph.TradingAgentsGraph", boom)
        out = deep_review.run_deep_review("000725", "2026-08-04")
        assert out["ok"] is False


class TestLoadDeepReviews:
    def test_lists_newest_first_and_filters(self, dr_dir, monkeypatch):
        monkeypatch.setattr("tradingagents.graph.trading_graph.TradingAgentsGraph",
                            _FakeGraph)
        deep_review.run_deep_review("300750", "2026-08-03", reason="a")
        deep_review.run_deep_review("000725", "2026-08-04", reason="b")
        all_recs = deep_review.load_deep_reviews()
        assert len(all_recs) == 2
        assert all_recs[0]["ticker"] == "000725"  # 最新在前
        only = deep_review.load_deep_reviews(ticker="300750")
        assert [r["ticker"] for r in only] == ["300750"]

    def test_limit(self, dr_dir, monkeypatch):
        monkeypatch.setattr("tradingagents.graph.trading_graph.TradingAgentsGraph",
                            _FakeGraph)
        for i in range(3):
            deep_review.run_deep_review("300750", f"2026-08-0{i + 1}", reason="x")
        assert len(deep_review.load_deep_reviews(limit=2)) == 2


class TestExtractPicks:
    def test_main_patterns(self):
        text = ("TOP1: 300750 宁德时代\nTOP2: 000725 京东方A\n"
                "TOP3: 688017 绿的谐波\nTOP4: 603256 宏和科技\n")
        assert extract_picks(text, 3) == ["300750", "000725", "688017"]
        assert extract_picks(text, 5) == ["300750", "000725", "688017", "603256"]

    def test_cyb_kcb_bj(self):
        text = "301234 次新 / 832000 北交 / 920111 北交 / 002594 比亚迪"
        assert extract_picks(text, 5) == ["301234", "832000", "920111", "002594"]

    def test_filters_false_positives(self):
        text = ("日期 2026-08-04，总资金 100000 元，目标价 300750.5？不，价格 12.34，"
                "数量 500000 股，止损 -5%")
        assert extract_picks(text) == []

    def test_dedup_keeps_order(self):
        assert extract_picks("300750 和 000725 再提 300750", 3) == ["300750", "000725"]

    def test_empty(self):
        assert extract_picks("") == []
        assert extract_picks(None) == []

    def test_truncated_picks(self):
        text = "600519 / 000858 / 300059 / 688111"
        assert extract_picks(text, 2) == ["600519", "000858"]
