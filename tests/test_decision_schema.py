"""决策卡契约校验 + pipeline 反馈重试单测（mock LLM，无真实调用）。"""

from tradingagents.shortterm import pipeline
from tradingagents.shortterm.schema import validate_decision_card

VALID_CARD = """**方向**: 买入
**置信度**: 中
**适用周期**: 3-10日波段

### 逻辑链
1. 表面原因：放量突破
2. 深层逻辑：资金驱动
3. 时间窗口：催化初期
4. 反向思考：缩量则失效

### 交易计划
- 介入条件: 回踩10日线企稳
- 目标价: 12.0 / 13.5
- 预期周期: 5个交易日
- 仓位建议: 不超过20%
- 止损位: 9.5
- 失效条件: 跌破20日线
- 次日观察点: 竞价量能

### 机构行为反向推理
未见明显出货信号。
"""


class TestValidateDecisionCard:
    def test_valid_card_passes(self):
        assert validate_decision_card(VALID_CARD, "swing") == []

    def test_missing_direction(self):
        bad = VALID_CARD.replace("**方向**: 买入\n", "")
        violations = validate_decision_card(bad, "swing")
        assert any("方向" in v for v in violations)

    def test_illegal_direction_value(self):
        bad = VALID_CARD.replace("**方向**: 买入", "**方向**: 强烈买入")
        violations = validate_decision_card(bad, "swing")
        assert any("方向" in v and "非法" in v for v in violations)

    def test_missing_confidence(self):
        bad = VALID_CARD.replace("**置信度**: 中\n", "")
        violations = validate_decision_card(bad, "swing")
        assert any("置信度" in v for v in violations)

    def test_illegal_confidence_value(self):
        bad = VALID_CARD.replace("**置信度**: 中", "**置信度**: 比较高")
        violations = validate_decision_card(bad, "swing")
        assert any("置信度" in v and "非法" in v for v in violations)

    def test_missing_required_section(self):
        bad = VALID_CARD.replace("### 机构行为反向推理", "### 机构行为")
        violations = validate_decision_card(bad, "swing")
        assert any("机构行为反向推理" in v for v in violations)

    def test_missing_plan_item(self):
        bad = VALID_CARD.replace("- 止损位: 9.5\n", "")
        violations = validate_decision_card(bad, "swing")
        assert any("止损位" in v for v in violations)

    def test_quick_mode_skipped(self):
        assert validate_decision_card("随便一段文字", "quick") == []
        assert validate_decision_card("", "blacklist") == []


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


def _run_with_llm(monkeypatch, outputs):
    calls = {"n": 0, "prompts": []}

    class _FakeLLM:
        def invoke(self, prompt):
            calls["prompts"].append(prompt)
            out = outputs[min(calls["n"], len(outputs) - 1)]
            calls["n"] += 1
            return type("R", (), {"content": out})()

    monkeypatch.setattr(pipeline, "run_ch0", lambda t, d: _ch0_pass())
    monkeypatch.setattr(pipeline, "gather_data_bundle", lambda *a: "BUNDLE")
    monkeypatch.setattr(pipeline, "build_llm", lambda *a, **k: _FakeLLM())
    monkeypatch.setattr(pipeline, "load_past_evaluations", lambda t, d: [])

    result = pipeline.run("000725", "2026-07-10")
    return result, calls


class TestPipelineValidation:
    def test_valid_first_try_no_retry(self, monkeypatch):
        result, calls = _run_with_llm(monkeypatch, [VALID_CARD])
        assert calls["n"] == 1
        assert result["validation"] == {"ok": True, "violations": [], "retried": False,
                                        "unanchored": []}
        assert result["report"] == VALID_CARD

    def test_retry_on_invalid_then_pass(self, monkeypatch):
        bad = "**方向**: 观望\n**置信度**: 低"
        result, calls = _run_with_llm(monkeypatch, [bad, VALID_CARD])
        assert calls["n"] == 2
        assert result["validation"]["ok"] is True
        assert result["validation"]["retried"] is True
        assert result["report"] == VALID_CARD
        # 反馈 prompt 含违规清单与上次输出
        assert "格式校验未通过" in calls["prompts"][1]
        assert bad in calls["prompts"][1]

    def test_retry_exhausted_keeps_report_marks_failure(self, monkeypatch):
        bad = "**方向**: 观望\n**置信度**: 低"
        result, calls = _run_with_llm(monkeypatch, [bad, bad])
        assert calls["n"] == 2
        assert result["validation"]["ok"] is False
        assert result["validation"]["retried"] is True
        assert result["validation"]["violations"]
        assert result["report"] == bad  # 报告仍返回，不阻塞主流程

    def test_retry_llm_failure_keeps_original(self, monkeypatch):
        bad = "**方向**: 观望\n**置信度**: 低"
        calls = {"n": 0}

        class _FlakyLLM:
            def invoke(self, prompt):
                calls["n"] += 1
                if calls["n"] == 2:
                    raise RuntimeError("LLM down")
                return type("R", (), {"content": bad})()

        monkeypatch.setattr(pipeline, "run_ch0", lambda t, d: _ch0_pass())
        monkeypatch.setattr(pipeline, "gather_data_bundle", lambda *a: "BUNDLE")
        monkeypatch.setattr(pipeline, "build_llm", lambda *a, **k: _FlakyLLM())
        monkeypatch.setattr(pipeline, "load_past_evaluations", lambda t, d: [])

        result = pipeline.run("000725", "2026-07-10")
        assert result["report"] == bad
        assert result["validation"]["ok"] is False
