"""compare.py 规则对比 + 自动触发 + LLM 深度对比测试（无网络/无 LLM）。"""

import json
from unittest.mock import MagicMock

import pytest

from tradingagents.analysis_registry import compare, registry
from tradingagents.analysis_registry.compare import (
    after_register,
    build_llm_compare,
    build_rule_compare,
    build_timeline_compare,
    latest_compare_for,
    load_compare_reports,
    save_compare_report,
)


def _rec(ticker="000725", trade_date="2026-07-31", ts=100, id_=None,
         path="/x/000725.json", **summary):
    return {
        "id": id_ or f"stock:{ticker}:{trade_date}:{ts}",
        "kind": "stock", "ticker": ticker, "name": "京东方A",
        "trade_date": trade_date, "ts": ts, "path": path,
        "summary": summary,
    }


@pytest.fixture
def reg(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADINGAGENTS_REGISTRY_DIR", str(tmp_path))
    return tmp_path


class TestRuleCompare:
    def test_direction_flip(self):
        old = _rec(direction="买入", confidence="高", last_close=4.0)
        new = _rec(ts=101, direction="观望", confidence="高", last_close=5.42)
        out = build_rule_compare(old, new)
        assert out["ticker"] == "000725"
        row = next(r for r in out["key_metrics"] if r["field"] == "方向")
        assert row["change"] == "flip"
        assert row["old"] == "买入" and row["new"] == "观望"
        assert "方向：买入 → 观望" in out["changes"]

    def test_px_percent_diff(self):
        out = build_rule_compare(_rec(last_close=4.0), _rec(ts=101, last_close=5.42))
        assert "收盘价：4.0 → 5.42（+35.50%）" in out["changes"]

    def test_new_and_gone_fields(self):
        out = build_rule_compare(_rec(direction="买入"),
                                 _rec(ts=101, direction="买入", rating="Buy"))
        row = next(r for r in out["key_metrics"] if r["field"] == "评级")
        assert row["change"] == "new"
        assert "评级：新增 Buy" in out["changes"]
        out2 = build_rule_compare(_rec(direction="买入", rating="Buy"),
                                  _rec(ts=101, direction="买入"))
        row2 = next(r for r in out2["key_metrics"] if r["field"] == "评级")
        assert row2["change"] == "gone"
        assert "评级：Buy → 消失" in out2["changes"]

    def test_same_fields(self):
        out = build_rule_compare(_rec(direction="买入", confidence="高"),
                                 _rec(ts=101, direction="买入", confidence="高"))
        assert "方向" in out["same_fields"] and "置信度" in out["same_fields"]
        assert out["changes"] == []

    def test_anomaly_diff(self):
        out = build_rule_compare(_rec(direction="买入", anomaly_types=["limit_up"]),
                                 _rec(ts=101, direction="买入",
                                      anomaly_types=["limit_up", "overheat"]))
        row = next(r for r in out["key_metrics"] if r["field"] == "异动")
        assert row["change"] == "changed"
        assert "新增异动：overheat" in out["changes"]

    def test_same_id_returns_none(self):
        assert build_rule_compare(_rec(), _rec()) is None

    def test_missing_both_values_skipped(self):
        out = build_rule_compare(_rec(direction="买入"), _rec(ts=101, direction="买入"))
        assert all(r["field"] != "适用周期" for r in out["key_metrics"])


class TestTimelineCompare:
    def test_columns_and_adjacent(self):
        recs = [_rec(ts=100, direction="买入"),
                _rec(ts=101, direction="观望"),
                _rec(ts=102, direction="卖出", rating="Sell")]
        out = build_timeline_compare(recs)
        assert [r["id"] for r in out["records"]] == [r["id"] for r in recs]
        assert out["columns"]["direction"]["values"] == ["买入", "观望", "卖出"]
        assert len(out["adjacent"]) == 2
        assert out["adjacent"][0]["newer"]["ts"] == 101
        assert out["adjacent"][1]["newer"]["ts"] == 102

    def test_sorted_by_date(self):
        recs = [_rec(trade_date="2026-08-03", ts=200, direction="卖出"),
                _rec(trade_date="2026-07-31", ts=100, direction="买入")]
        out = build_timeline_compare(recs)
        assert out["records"][0]["trade_date"] == "2026-07-31"


class TestPersistence:
    def test_save_and_load(self, reg):
        payload = build_rule_compare(_rec(ts=100), _rec(ts=101))
        path = save_compare_report(payload, "000725")
        assert path is not None and path.exists()
        assert "000725_stock_000725_2026-07-31_100__vs__stock_000725_2026-07-31_101" \
            in path.name
        reports = load_compare_reports()
        assert len(reports) == 1
        assert load_compare_reports("000725") and not load_compare_reports("600519")
        assert latest_compare_for("000725")["newer"]["ts"] == 101

    def test_save_idempotent(self, reg):
        payload = build_rule_compare(_rec(ts=100), _rec(ts=101))
        p1 = save_compare_report(payload, "000725")
        p2 = save_compare_report(payload, "000725")
        assert p1 == p2
        assert len(load_compare_reports()) == 1


class TestAutoTrigger:
    def test_register_triggers_compare(self, reg):
        # 直接用 register 写两条同票记录 → 自动生成对比
        registry.register("stock", "000725", "2026-07-31", ts=100,
                          path="/a.json", summary={"direction": "买入"})
        registry.register("stock", "000725", "2026-08-03", ts=150,
                          path="/b.json", summary={"direction": "观望"})
        reports = load_compare_reports("000725")
        assert len(reports) == 1
        assert reports[0]["newer"]["ts"] == 150
        assert reports[0]["changes"] == ["方向：买入 → 观望"]

    def test_register_no_auto_for_screen_or_first(self, reg):
        registry.register("stock", "000725", "2026-07-31", ts=100,
                          path="/a.json", summary={})
        assert load_compare_reports("000725") == []
        registry.register("screen", "", "2026-08-01", ts=200, path="/s.json")
        assert load_compare_reports() == []

    def test_after_register_single_record_noop(self, reg):
        registry.register("stock", "000725", "2026-07-31", ts=100,
                          path="/a.json", summary={})
        after_register("stock", "000725")
        assert load_compare_reports() == []


class TestLLMCompare:
    def _patch_llm(self, monkeypatch, text="B 转向谨慎"):
        def _fake_build_llm(provider, model, base_url=None):
            llm = MagicMock()
            resp = MagicMock()
            resp.content = text
            llm.invoke.return_value = resp
            return llm
        monkeypatch.setattr("tradingagents.shortterm.pipeline.build_llm",
                            _fake_build_llm)
        return llm if False else None

    def test_success(self, reg, monkeypatch, tmp_path):
        p1, p2 = tmp_path / "a.json", tmp_path / "b.json"
        p1.write_text(json.dumps({"direction": "买入"}), encoding="utf-8")
        p2.write_text(json.dumps({"direction": "观望"}), encoding="utf-8")
        self._patch_llm(monkeypatch)
        rec_a = _rec(ts=100, path=str(p1))
        rec_b = _rec(ts=101, path=str(p2))
        out = build_llm_compare(rec_a, rec_b)
        print('DBG', out)
        assert out["ok"] is True
        assert out["text"] == "B 转向谨慎"
        assert "stock 000725" in out["meta_a"]

    def test_unreadable_reports(self, reg, tmp_path):
        out = build_llm_compare(_rec(ts=100, path="/no/a.json"),
                                _rec(ts=101, path="/no/b.json"))
        assert out["ok"] is False
        assert "无可读原文" in out["error"]

    def test_llm_error_degrades(self, reg, monkeypatch, tmp_path):
        p1, p2 = tmp_path / "a.json", tmp_path / "b.json"
        p1.write_text("x", encoding="utf-8")
        p2.write_text("y", encoding="utf-8")

        def _fail_build_llm(provider, model, base_url=None):
            raise RuntimeError("boom")
        monkeypatch.setattr("tradingagents.shortterm.pipeline.build_llm",
                            _fail_build_llm)
        rec_a = _rec(ts=100, path=str(p1))
        rec_b = _rec(ts=101, path=str(p2))
        out = build_llm_compare(rec_a, rec_b)
        assert out["ok"] is False
        assert "boom" in out["error"]
