"""断言规则库（禁用术语）+ 数值锚定校验单测。"""

import json

from tradingagents.shortterm import assertions, pipeline
from tradingagents.shortterm.anchors import check_numeric_anchors

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


class TestAssertions:
    def test_clean_card_passes(self, monkeypatch, tmp_path):
        monkeypatch.setattr(assertions, "_USER_RULES_PATH", tmp_path / "none.json")
        assert assertions.check_assertions(VALID_CARD) == []

    def test_builtin_banned_terms(self, monkeypatch, tmp_path):
        monkeypatch.setattr(assertions, "_USER_RULES_PATH", tmp_path / "none.json")
        for term in ("建议买入", "建议卖出", "稳赚不赔", "必涨"):
            bad = VALID_CARD + f"\n补充：{term}。"
            violations = assertions.check_assertions(bad)
            assert any(term in v for v in violations), term

    def test_direction_enum_not_flagged(self, monkeypatch, tmp_path):
        """决策卡自身的'方向: 买入'不触发'建议买入'类误报。"""
        monkeypatch.setattr(assertions, "_USER_RULES_PATH", tmp_path / "none.json")
        assert assertions.check_assertions(VALID_CARD) == []

    def test_user_rules_extend(self, monkeypatch, tmp_path):
        cfg = tmp_path / "assertions.json"
        cfg.write_text(json.dumps({
            "banned_terms": ["满仓干"],
            "banned_patterns": [r"收益保证"],
        }), encoding="utf-8")
        monkeypatch.setattr(assertions, "_USER_RULES_PATH", cfg)
        assertions._RULES_CACHE.clear()

        bad = VALID_CARD + "\n补充：可以满仓干，收益保证。"
        violations = assertions.check_assertions(bad)
        assert any("满仓干" in v for v in violations)
        assert any("收益保证" in v for v in violations)

    def test_user_rules_malformed_ignored(self, monkeypatch, tmp_path):
        cfg = tmp_path / "assertions.json"
        cfg.write_text("{not json", encoding="utf-8")
        monkeypatch.setattr(assertions, "_USER_RULES_PATH", cfg)
        assertions._RULES_CACHE.clear()
        assert assertions.check_assertions(VALID_CARD) == []


class TestNumericAnchors:
    SOURCE = """### 近期行情
2026-07-10 收盘 10.0 元，PE 25.3，换手率 5.0%
### 技术指标
MACD 金叉，RSI 62.5"""

    def test_anchored_numbers_pass(self):
        report = "PE 25.3 处于合理区间，收盘 10.0 元。"
        assert check_numeric_anchors(report, self.SOURCE) == []

    def test_unanchored_fundamental_flagged(self):
        report = "该公司 ROE 18.5%，基本面优秀。"
        violations = check_numeric_anchors(report, self.SOURCE)
        assert any("ROE" in v and "18.5" in v for v in violations)

    def test_tolerance_rounding(self):
        """源数据 25.32 报告写 25.3 → 视为锚定。"""
        assert check_numeric_anchors("PE 25.3 倍", "PE 25.32") == []

    def test_plan_levels_not_flagged(self):
        """交易计划里的目标价/止损位属推演值，不参与锚定。"""
        report = ("PE 25.3 合理。\n- 目标价: 12.0\n- 止损位: 9.5\n"
                  "- 介入条件: 9.8 以下")
        assert check_numeric_anchors(report, self.SOURCE) == []


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


class TestPipelineAssertions:
    def test_banned_term_triggers_retry(self, monkeypatch, tmp_path):
        monkeypatch.setattr(assertions, "_USER_RULES_PATH", tmp_path / "none.json")
        assertions._RULES_CACHE.clear()
        outputs = [VALID_CARD + "\n综上，建议买入。", VALID_CARD]
        calls = {"n": 0}

        class _FakeLLM:
            def invoke(self, prompt):
                out = outputs[min(calls["n"], 1)]
                calls["n"] += 1
                return type("R", (), {"content": out})()

        monkeypatch.setattr(pipeline, "run_ch0", lambda t, d: _ch0_pass())
        monkeypatch.setattr(pipeline, "gather_data_bundle", lambda *a: "BUNDLE")
        monkeypatch.setattr(pipeline, "build_llm", lambda *a, **k: _FakeLLM())
        monkeypatch.setattr(pipeline, "load_past_evaluations", lambda t, d: [])

        result = pipeline.run("000725", "2026-07-10")
        assert calls["n"] == 2
        assert result["validation"]["ok"] is True
        assert result["report"] == VALID_CARD

    def test_unanchored_soft_warning_no_retry(self, monkeypatch, tmp_path):
        """数值未锚定 → 软告警，不触发重试、不影响 ok。"""
        monkeypatch.setattr(assertions, "_USER_RULES_PATH", tmp_path / "none.json")
        assertions._RULES_CACHE.clear()
        card = VALID_CARD + "\n公司 ROE 18.5%，质地优秀。"

        class _FakeLLM:
            def invoke(self, prompt):
                return type("R", (), {"content": card})()

        monkeypatch.setattr(pipeline, "run_ch0", lambda t, d: _ch0_pass())
        monkeypatch.setattr(pipeline, "gather_data_bundle", lambda *a: "BUNDLE")
        monkeypatch.setattr(pipeline, "build_llm", lambda *a, **k: _FakeLLM())
        monkeypatch.setattr(pipeline, "load_past_evaluations", lambda t, d: [])

        result = pipeline.run("000725", "2026-07-10")
        assert result["validation"]["ok"] is True
        assert result["validation"]["retried"] is False
        assert any("ROE" in u for u in result["validation"]["unanchored"])
