"""决策卡解析扩展单测：适用周期 + 展示色映射。"""

from tradingagents.shortterm.history import parse_decision
from web.components.decision_card import direction_style


class TestParseHorizon:
    def test_full_card(self):
        report = "**方向**: 买入\n**置信度**: 高\n**适用周期**: 3-10日波段\n正文"
        out = parse_decision(report)
        assert out == {"direction": "买入", "confidence": "高",
                       "horizon": "3-10日波段"}

    def test_ultra_short(self):
        report = "**方向**: 观望\n**置信度**: 低\n**适用周期**: 隔日超短"
        out = parse_decision(report)
        assert out["horizon"] == "隔日超短"

    def test_missing_horizon(self):
        report = "**方向**: 卖出\n**置信度**: 中"
        out = parse_decision(report)
        assert out["horizon"] is None

    def test_fullwidth_colon(self):
        report = "**方向**：回避\n**置信度**：中\n**适用周期**：隔日超短"
        out = parse_decision(report)
        assert out["direction"] == "回避"
        assert out["horizon"] == "隔日超短"

    def test_empty(self):
        assert parse_decision("") == {"direction": None, "confidence": None,
                                      "horizon": None}


class TestDirectionStyle:
    def test_known_directions(self):
        assert direction_style("买入")["color"] == "#ff4b4b"
        assert direction_style("卖出")["color"] == "#21c354"
        assert direction_style("观望")["color"] == "#888888"
        assert direction_style("回避")["color"] == "#ffa421"

    def test_unknown_fallback(self):
        assert direction_style(None)["color"] == "#888888"
        assert direction_style("其他")["color"] == "#888888"
