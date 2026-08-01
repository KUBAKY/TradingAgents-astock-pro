"""pipeline 单测：v2 自校准注入路径（mock 网络/LLM，无真实调用）。"""

from tradingagents.shortterm import pipeline


def _ch0_pass():
    return {
        "ticker": "000725", "name": "京东方A", "trade_date": "2026-07-10",
        "verdict": "PASS", "board": "main", "mcap_tier": "mid", "mcap_yi": 300.0,
        "blacklist": [],
        "metrics": {"last_close": 10.0, "ret_7d_pct": 5.0, "ret_30d_pct": 10.0,
                    "vol_ratio_vs_5d": 1.5, "amplitude_pct": 3.0,
                    "volatility_multiple": 1.0, "is_250d_high": False,
                    "is_250d_low": False, "recent_bars": []},
        "limit_streak": {"limit_up_streak": 0, "limit_down_streak": 0},
        "turnover_pct": 5.0, "lhb_appearances_10d": 0,
        "anomalies": [],
        "mode_hint": {"mode": "swing", "label": "3-10日波段", "reason": "x"},
        "data_gaps": [],
    }


def _run_pipeline(monkeypatch, past):
    captured = {}

    class _FakeLLM:
        def invoke(self, prompt):
            captured["prompt"] = prompt
            return type("R", (), {"content": "**方向**: 观望\n**置信度**: 低"})()

    monkeypatch.setattr(pipeline, "run_ch0", lambda t, d: _ch0_pass())
    monkeypatch.setattr(pipeline, "gather_data_bundle", lambda *a: "BUNDLE")
    monkeypatch.setattr(pipeline, "build_llm", lambda *a, **k: _FakeLLM())
    monkeypatch.setattr(pipeline, "load_past_evaluations", lambda t, d: past)

    result = pipeline.run("000725", "2026-07-10")
    return result, captured["prompt"]


class TestHistoryInjection:
    PAST = [{
        "record": {"trade_date": "2026-06-30",
                   "parsed": {"direction": "买入", "confidence": "高"}},
        "evaluation": {"verdict": "错", "verdict_basis": "买入 vs T+3 收益 -3.46%",
                       "t1_close_pct": 1.04, "t3_close_pct": -3.46, "t10_close_pct": -19.12},
    }]

    def test_injected_when_history_exists(self, monkeypatch):
        _, prompt = _run_pipeline(monkeypatch, self.PAST)
        assert "你过去对该标的的判断及事后验证" in prompt
        assert "2026-06-30" in prompt
        assert "判定 错" in prompt
        assert "BUNDLE" in prompt

    def test_not_injected_when_empty(self, monkeypatch):
        _, prompt = _run_pipeline(monkeypatch, [])
        assert "你过去对该标的" not in prompt

    def test_history_failure_silent(self, monkeypatch):
        def boom(t, d):
            raise RuntimeError("disk error")
        captured = {}

        class _FakeLLM:
            def invoke(self, prompt):
                captured["prompt"] = prompt
                return type("R", (), {"content": "ok"})()

        monkeypatch.setattr(pipeline, "run_ch0", lambda t, d: _ch0_pass())
        monkeypatch.setattr(pipeline, "gather_data_bundle", lambda *a: "BUNDLE")
        monkeypatch.setattr(pipeline, "build_llm", lambda *a, **k: _FakeLLM())
        monkeypatch.setattr(pipeline, "load_past_evaluations", boom)

        result = pipeline.run("000725", "2026-07-10")
        assert result["report"] == "ok"  # 主流程不受影响
        assert "你过去对该标的" not in captured["prompt"]

    def test_result_contains_bundle(self, monkeypatch):
        result, _ = _run_pipeline(monkeypatch, [])
        assert result["bundle"] == "BUNDLE"
        assert result["mode"] == "swing"
