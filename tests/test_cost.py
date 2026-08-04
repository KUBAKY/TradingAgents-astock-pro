"""AI 成本真实计量模块测试：定价解析/匹配/刷新、账本、LLM 包装器。

单位：人民币（元）。价格源：DeepSeek 中文定价页。
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta

import pytest

from tradingagents.cost import ledger, pricing, tracker

# ── 真实页面片段（2026-08 抓取自 api-docs.deepseek.com/zh-cn/quick_start/pricing）──
FIXTURE_HTML = (
    '<div style="font-size:14px"><b><table style="text-align:center">'
    "<tr><td colspan=\"2\" style=\"text-align:center\">模型</td>"
    "<td>deepseek-v4-flash</td><td>deepseek-v4-pro</td></tr>"
    "<tr><td colspan=\"2\">模型版本</td><td>DeepSeek-V4-Flash-0731</td>"
    "<td>DeepSeek-V4-Pro</td></tr>"
    "<tr><td rowspan=\"3\">价格<sup>(2)</sup></td>"
    "<td>百万tokens输入（缓存命中）</td><td>0.02元</td><td>0.025元</td></tr>"
    "<tr><td>百万tokens输入（缓存未命中）</td><td>1元</td><td>3元</td></tr>"
    "<tr><td>百万tokens输出</td><td>2元</td><td>6元</td></tr>"
    "<tr><td colspan=\"2\">并发限制<sup>(3)</sup></td><td>2500</td><td>500</td></tr>"
    "</table></b></div>"
    "<p>(1) Responses API 目前仅支持 <code>deepseek-v4-flash</code> 模型，"
    "暂不支持 <code>deepseek-v4-pro</code> 模型。</p>"
)


@pytest.fixture(autouse=True)
def cost_dir(tmp_path, monkeypatch):
    """每个测试独立成本目录，不碰真实 ~/.tradingagents。"""
    monkeypatch.setenv("TRADINGAGENTS_COST_DIR", str(tmp_path))
    # 重置进程级自动刷新标记，保证测试间隔离
    monkeypatch.setattr(pricing, "_AUTO_REFRESH_ATTEMPTED", False)
    return tmp_path


def _write_cache(tmp_path, pricing_data, fetched_at_iso):
    (tmp_path / "pricing.json").write_text(
        json.dumps({"pricing": pricing_data, "fetched_at": fetched_at_iso,
                    "source": "test"}), encoding="utf-8")


# ── 定价表 ──────────────────────────────────────────────────────────────────

class TestPricingDefaults:
    def test_builtin_table_has_v4_models(self):
        assert pricing.DEFAULT_PRICING["deepseek-v4-flash"] == {
            "input_hit": 0.02, "input_miss": 1.0, "output": 2.0}
        assert pricing.DEFAULT_PRICING["deepseek-v4-pro"] == {
            "input_hit": 0.025, "input_miss": 3.0, "output": 6.0}

    def test_get_price_exact_match(self, cost_dir):
        assert pricing.get_price("deepseek-v4-flash")["output"] == 2.0
        assert pricing.get_price("deepseek-v4-pro")["input_miss"] == 3.0

    def test_get_price_prefix_match(self, cost_dir):
        assert pricing.get_price("deepseek-v4-flash-0731")["output"] == 2.0
        assert pricing.get_price("deepseek-v4-pro@202608")["input_miss"] == 3.0

    def test_get_price_unknown_model(self, cost_dir):
        assert pricing.get_price("deepseek-chat") is None
        assert pricing.get_price("gpt-4o") is None


class TestParsePage:
    def test_parse_real_page_fixture(self):
        parsed = pricing.parse_pricing_page(FIXTURE_HTML)
        assert parsed["deepseek-v4-flash"] == {"input_hit": 0.02, "input_miss": 1.0, "output": 2.0}
        assert parsed["deepseek-v4-pro"] == {"input_hit": 0.025, "input_miss": 3.0, "output": 6.0}

    def test_parse_garbage_raises(self):
        with pytest.raises(pricing.ParseError):
            pricing.parse_pricing_page("<html>什么都没有</html>")

    def test_parse_missing_price_category_raises(self):
        partial = FIXTURE_HTML.replace("百万tokens输入（缓存命中）", "不见了")
        with pytest.raises(pricing.ParseError):
            pricing.parse_pricing_page(partial)


class TestRefresh:
    def test_refresh_writes_cache(self, cost_dir, monkeypatch):
        monkeypatch.setattr(pricing, "_http_get", lambda url, timeout=20: FIXTURE_HTML)
        parsed, meta = pricing.refresh_pricing()
        assert parsed["deepseek-v4-flash"]["output"] == 2.0
        assert meta["fetched_at"]
        assert (cost_dir / "pricing.json").exists()

    def test_refresh_network_error_raises(self, cost_dir, monkeypatch):
        def boom(url, timeout=20):
            raise RuntimeError("network down")
        monkeypatch.setattr(pricing, "_http_get", boom)
        with pytest.raises(RuntimeError):
            pricing.refresh_pricing()

    def test_auto_refresh_when_stale(self, cost_dir, monkeypatch):
        calls = {"n": 0}

        def fake_get(url, timeout=20):
            calls["n"] += 1
            return FIXTURE_HTML
        monkeypatch.setattr(pricing, "_http_get", fake_get)
        old = (datetime.now() - timedelta(days=10)).isoformat(timespec="seconds")
        _write_cache(cost_dir, pricing.DEFAULT_PRICING, old)

        price = pricing.get_price("deepseek-v4-flash")
        assert price["output"] == 2.0
        assert calls["n"] == 1
        # 缓存已被刷新 → 不再 stale
        assert pricing.pricing_info()["stale"] is False

    def test_auto_refresh_failure_falls_back_and_no_retry(self, cost_dir, monkeypatch):
        calls = {"n": 0}

        def boom(url, timeout=20):
            calls["n"] += 1
            raise RuntimeError("down")
        monkeypatch.setattr(pricing, "_http_get", boom)
        old = (datetime.now() - timedelta(days=10)).isoformat(timespec="seconds")
        _write_cache(cost_dir, pricing.DEFAULT_PRICING, old)

        assert pricing.get_price("deepseek-v4-flash") is not None  # 用旧缓存
        assert pricing.get_price("deepseek-v4-flash") is not None
        assert calls["n"] == 1  # 每进程最多尝试一次

    def test_fresh_cache_no_auto_refresh(self, cost_dir, monkeypatch):
        calls = {"n": 0}
        monkeypatch.setattr(pricing, "_http_get", lambda url, timeout=20: calls.__setitem__("n", calls["n"] + 1) or FIXTURE_HTML)
        _write_cache(cost_dir, pricing.DEFAULT_PRICING,
                     datetime.now().isoformat(timespec="seconds"))
        pricing.get_price("deepseek-v4-flash")
        assert calls["n"] == 0

    def test_pricing_info_stale_flag(self, cost_dir):
        assert pricing.pricing_info()["stale"] is True  # 无缓存
        _write_cache(cost_dir, pricing.DEFAULT_PRICING,
                     datetime.now().isoformat(timespec="seconds"))
        assert pricing.pricing_info()["stale"] is False


# ── 账本 ────────────────────────────────────────────────────────────────────

class TestLedger:
    def test_append_and_read_roundtrip(self, cost_dir):
        ledger.append_entry({"feature": "shortterm", "model": "deepseek-v4-flash",
                             "input": 100, "output": 50, "cost_cny": 0.001})
        ents = ledger.read_entries()
        assert len(ents) == 1
        assert ents[0]["model"] == "deepseek-v4-flash"
        assert ents[0]["ts"] > 0  # 自动补 ts

    def test_summarize_periods(self, cost_dir):
        ref = datetime(2026, 8, 5, 12, 0)  # 周三；本周=8/3(一)起，本月=8月
        entries = [
            (ref.timestamp(), 0.01),                        # 今日
            ((ref - timedelta(days=1)).timestamp(), 0.02),  # 今日+本周+本月（8-04 周二）
            ((ref - timedelta(days=3)).timestamp(), 0.03),  # 本月但上周（8-02 周日）
            ((ref - timedelta(days=100)).timestamp(), 0.99),  # 超90天 → 归档
        ]
        for ts, cost in entries:
            ledger.append_entry({"feature": "shortterm", "model": "m",
                                 "input": 1000, "output": 1000,
                                 "cost_cny": cost, "ts": ts})
        assert ledger.summarize("today", now=ref)["total_cost_cny"] == pytest.approx(0.01, abs=1e-6)
        assert ledger.summarize("week", now=ref)["total_cost_cny"] == pytest.approx(0.03, abs=1e-6)
        assert ledger.summarize("month", now=ref)["total_cost_cny"] == pytest.approx(0.06, abs=1e-6)
        assert ledger.summarize("week", now=ref)["calls"] == 2

    def test_summarize_unpriced_tokens(self, cost_dir):
        ledger.append_entry({"feature": "main_graph", "model": "deepseek-chat",
                             "input": 5000, "output": 2000, "cost_cny": None})
        s = ledger.summarize("today")
        assert s["total_cost_cny"] == 0.0
        assert s["total_tokens"] == 7000
        assert s["rows"][0]["unpriced_tokens"] == 7000

    def test_summarize_groups_by_feature(self, cost_dir):
        ledger.append_entry({"feature": "shortterm", "cost_cny": 0.01, "input": 1, "output": 1})
        ledger.append_entry({"feature": "screener", "cost_cny": 0.02, "input": 1, "output": 1})
        s = ledger.summarize("today")
        assert {r["feature"] for r in s["rows"]} == {"shortterm", "screener"}

    def test_recent_filters(self, cost_dir):
        for i in range(3):
            ledger.append_entry({"feature": "shortterm", "run_id": "A",
                                 "cost_cny": 0.01 * i, "input": 1, "output": 1})
        ledger.append_entry({"feature": "screener", "run_id": "B",
                             "cost_cny": 0.5, "input": 1, "output": 1})
        assert len(ledger.recent(feature="shortterm")) == 3
        assert len(ledger.recent(run_id="B")) == 1
        assert len(ledger.recent(run_id="A", limit=2)) == 2

    def test_rotation_archives_old_lines(self, cost_dir):
        old = (datetime.now() - timedelta(days=100)).timestamp()
        ledger.append_entry({"feature": "x", "cost_cny": 0.01, "ts": old})
        ledger.append_entry({"feature": "x", "cost_cny": 0.02})
        ents = ledger.read_entries()
        assert len(ents) == 1  # 旧行已归档
        archive = cost_dir / "ledger_archive.jsonl"
        assert archive.exists()
        archived = [json.loads(l) for l in archive.read_text(encoding="utf-8").splitlines() if l.strip()]
        assert len(archived) == 1
        assert archived[0]["cost_cny"] == 0.01


# ── 成本计算 / 用量提取 ─────────────────────────────────────────────────────

class TestUsageExtraction:
    def test_deepseek_raw_token_usage(self):
        resp = type("R", (), {})()
        resp.response_metadata = {"token_usage": {
            "prompt_tokens": 1000, "completion_tokens": 500,
            "prompt_cache_hit_tokens": 700, "prompt_cache_miss_tokens": 300}}
        u = tracker.extract_usage(resp)
        assert u == {"input": 1000, "output": 500, "cache_hit": 700, "cache_miss": 300}

    def test_langchain_usage_metadata(self):
        resp = type("R", (), {})()
        resp.usage_metadata = {"input_tokens": 1000, "output_tokens": 500,
                               "input_token_details": {"cache_read": 700, "cache_creation": 300}}
        u = tracker.extract_usage(resp)
        assert u["cache_hit"] == 700 and u["cache_miss"] == 300

    def test_anthropic_usage_attr(self):
        resp = type("R", (), {})()
        resp.usage = {"input_tokens": 800, "output_tokens": 200}
        u = tracker.extract_usage(resp)
        assert u["input"] == 800 and u["cache_hit"] == 0 and u["cache_miss"] == 800

    def test_no_usage_info(self):
        resp = type("R", (), {})()
        resp.content = "hi"
        assert tracker.extract_usage(resp) is None
        assert tracker.extract_usage(None) is None

    def test_bad_usage_types_do_not_crash(self):
        resp = type("R", (), {})()
        resp.usage_metadata = {"input_tokens": "abc", "output_tokens": []}
        assert tracker.extract_usage(resp) is None


class TestCostCalc:
    def test_hit_miss_split(self):
        usage = {"input": 1000, "output": 500, "cache_hit": 700, "cache_miss": 300}
        price = pricing.DEFAULT_PRICING["deepseek-v4-flash"]
        # (700*0.02 + 300*1 + 500*2)/1e6 = 1314/1e6
        assert tracker.compute_cost(usage, price) == pytest.approx(0.001314, abs=1e-9)

    def test_no_price_returns_none(self):
        usage = {"input": 1000, "output": 500, "cache_hit": 0, "cache_miss": 1000}
        assert tracker.compute_cost(usage, None) is None
        assert tracker.compute_cost(None, pricing.DEFAULT_PRICING["deepseek-v4-flash"]) is None

    def test_missing_miss_treats_input_as_miss(self):
        usage = {"input": 1000, "output": 500, "cache_hit": 900, "cache_miss": None}
        price = pricing.DEFAULT_PRICING["deepseek-v4-flash"]
        # (900*0.02 + 100*1 + 500*2)/1e6 = 1118/1e6
        assert tracker.compute_cost(usage, price) == pytest.approx(0.001118, abs=1e-9)


# ── LLM 包装器 ──────────────────────────────────────────────────────────────

class _Resp:
    def __init__(self, usage=None):
        self.content = "ok"
        self.response_metadata = ({"token_usage": usage} if usage else {})


class _FakeLLM:
    model = "deepseek-v4-flash"

    def __init__(self, usage=None):
        self.usage = usage or {"prompt_tokens": 1000, "completion_tokens": 500,
                               "prompt_cache_hit_tokens": 700, "prompt_cache_miss_tokens": 300}

    def invoke(self, input, config=None, **kwargs):
        return _Resp(self.usage)

    async def ainvoke(self, input, config=None, **kwargs):
        return _Resp(self.usage)

    def with_structured_output(self, schema, **kwargs):
        return _FakeLLM(self.usage)

    def bind(self, **kwargs):
        return _FakeLLM(self.usage)


class TestTracker:
    def test_cost_context_sets_values(self):
        toks = tracker.cost_context("shortterm", "run-1")
        assert tracker.current_feature() == "shortterm"
        assert tracker.current_run_id() == "run-1"
        tracker.reset_cost_context(toks)
        assert tracker.current_feature() == "main_graph"

    def test_wrap_tracks_invoke(self, cost_dir):
        toks = tracker.cost_context("shortterm", "run-abc")
        wrapped = tracker.wrap_llm(_FakeLLM(), "deepseek", "deepseek-v4-flash")
        out = wrapped.invoke("hello")
        tracker.reset_cost_context(toks)
        assert out.content == "ok"
        ents = ledger.recent(run_id="run-abc")
        assert len(ents) == 1
        assert ents[0]["feature"] == "shortterm"
        assert ents[0]["cache_hit"] == 700
        assert ents[0]["cost_cny"] == pytest.approx(0.001314, abs=1e-9)

    def test_wrap_unpriced_model_tokens_only(self, cost_dir):
        wrapped = tracker.wrap_llm(_FakeLLM(), "deepseek", "deepseek-chat")
        wrapped.invoke("hi")
        ents = ledger.read_entries()
        assert ents[0]["cost_cny"] is None
        assert ents[0]["input"] == 1000

    def test_wrap_with_structured_output_tracks(self, cost_dir):
        wrapped = tracker.wrap_llm(_FakeLLM(), "deepseek", "deepseek-v4-flash")
        inner = wrapped.with_structured_output(dict)
        inner.invoke("hi")
        assert len(ledger.read_entries()) == 1

    def test_wrap_async_ainvoke_tracks(self, cost_dir):
        wrapped = tracker.wrap_llm(_FakeLLM(), "deepseek", "deepseek-v4-flash")
        asyncio.run(wrapped.ainvoke("hi"))
        assert len(ledger.read_entries()) == 1

    def test_internal_failure_does_not_block(self, cost_dir):
        class _BadResp:
            content = "ok"
            response_metadata = {"token_usage": {"prompt_tokens": "nan"}}
            usage_metadata = {"input_tokens": "abc"}

        class _BadLLM:
            model = "deepseek-v4-flash"

            def invoke(self, input, config=None, **kwargs):
                return _BadResp()
        wrapped = tracker.wrap_llm(_BadLLM(), "deepseek", "deepseek-v4-flash")
        out = wrapped.invoke("hi")  # 不抛异常
        assert out.content == "ok"
        # 仍记账（用量未知 → 全部 None，cost None）
        assert ledger.read_entries()[0]["cost_cny"] is None

    def test_run_summary(self, cost_dir):
        toks = tracker.cost_context("shortterm", "run-x")
        w = tracker.wrap_llm(_FakeLLM(), "deepseek", "deepseek-v4-flash")
        w.invoke("a")
        w.invoke("b")
        tracker.reset_cost_context(toks)
        s = tracker.run_summary("run-x")
        assert s["calls"] == 2
        assert s["total_cost_cny"] == pytest.approx(0.001314 * 2, abs=1e-9)
        assert s["total_tokens"] == 3000
        assert tracker.run_summary("nope") is None


# ── 接入点集成 ──────────────────────────────────────────────────────────────

class TestIntegration:
    def test_pipeline_build_llm_returns_wrapped(self, cost_dir, monkeypatch):
        from tradingagents.shortterm import pipeline

        class _FakeClient:
            def __init__(self, model):
                self.model = model

            def get_provider_name(self):
                return "deepseek"

            def get_llm(self):
                return _FakeLLM()

        monkeypatch.setattr(pipeline, "create_llm_client", lambda *a, **k: _FakeClient(k.get("model", "m")))
        llm = pipeline.build_llm("deepseek", "deepseek-v4-flash", None)
        assert isinstance(llm, tracker.TrackedLLM)
        toks = tracker.cost_context("shortterm", "run-p")
        llm.invoke("hi")
        tracker.reset_cost_context(toks)
        assert ledger.recent(run_id="run-p")

    def test_pipeline_build_llm_wraps_anthropic_requests(self, cost_dir):
        from tradingagents.shortterm import pipeline
        llm = pipeline.build_llm("anthropic", "claude-haiku-4-5", None)
        assert isinstance(llm, tracker.TrackedLLM)
        assert llm.model == "claude-haiku-4-5"

    def test_screener_sets_feature_context(self, cost_dir, monkeypatch):
        from tradingagents.shortterm import screener
        captured = {}

        class _FakeLLM2:
            model = "deepseek-v4-flash"

            def invoke(self, prompt):
                captured["feature"] = tracker.current_feature()
                return _Resp({"prompt_tokens": 10, "completion_tokens": 5,
                              "prompt_cache_hit_tokens": 0,
                              "prompt_cache_miss_tokens": 10})

        monkeypatch.setattr(screener, "build_llm", lambda *a, **k: _FakeLLM2())
        scan = {"trade_date": "2026-08-03", "capital": 100000, "boards": {}, "rejected": {}}
        out = screener.recommend(scan, "deepseek", "deepseek-v4-flash")
        assert out == "ok"
        assert captured["feature"] == "screener"
