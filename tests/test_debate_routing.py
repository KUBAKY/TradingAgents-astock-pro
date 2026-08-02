"""多空辩论路由解耦 + 中文化测试。"""

from tradingagents.graph.conditional_logic import ConditionalLogic


def _state(count=1, speaker=None, response=""):
    ids = {"count": count, "current_response": response}
    if speaker is not None:
        ids["latest_speaker"] = speaker
    return {"investment_debate_state": ids}


class TestDebateRouting:
    def setup_method(self):
        self.logic = ConditionalLogic(max_debate_rounds=3)

    def test_bull_then_bear(self):
        assert self.logic.should_continue_debate(
            _state(speaker="Bull")) == "Bear Researcher"

    def test_bear_then_bull(self):
        assert self.logic.should_continue_debate(
            _state(speaker="Bear")) == "Bull Researcher"

    def test_chinese_prefix_does_not_leak_into_routing(self):
        # 中文前缀 + latest_speaker=Bull → 仍应轮到 Bear
        assert self.logic.should_continue_debate(
            _state(speaker="Bull", response="多方分析师: 看多论点")) == "Bear Researcher"

    def test_legacy_english_prefix_fallback(self):
        # 旧存档无 latest_speaker → 退回英文前缀解析
        assert self.logic.should_continue_debate(
            _state(response="Bull Analyst: argument")) == "Bear Researcher"

    def test_first_round_starts_with_bull(self):
        assert self.logic.should_continue_debate(
            _state(count=0, response="")) == "Bull Researcher"

    def test_max_rounds_goes_to_research_manager(self):
        assert self.logic.should_continue_debate(
            _state(count=6, speaker="Bull")) == "Research Manager"


class TestResearcherChineseAdaptation:
    def _state(self):
        return {
            "investment_debate_state": {
                "history": "", "bull_history": "", "bear_history": "",
                "current_response": "", "count": 0,
            },
            "market_report": "m", "sentiment_report": "s",
            "news_report": "n", "fundamentals_report": "f",
        }

    def _run(self, create_fn):
        from tradingagents.dataflows.config import set_config
        set_config({"output_language": "Chinese"})

        captured = {}

        class _LLM:
            def invoke(self, prompt):
                captured["prompt"] = prompt
                return type("R", (), {"content": "中文论点"})()

        node = create_fn(_LLM())
        return node(self._state()), captured

    def test_bull(self):
        from tradingagents.agents.researchers.bull_researcher import (
            create_bull_researcher,
        )
        out, captured = self._run(create_bull_researcher)
        ids = out["investment_debate_state"]
        assert ids["current_response"].startswith("多方分析师:")
        assert ids["latest_speaker"] == "Bull"
        assert "Write your entire response in Chinese" in captured["prompt"]

    def test_bear(self):
        from tradingagents.agents.researchers.bear_researcher import (
            create_bear_researcher,
        )
        out, captured = self._run(create_bear_researcher)
        ids = out["investment_debate_state"]
        assert ids["current_response"].startswith("空方分析师:")
        assert ids["latest_speaker"] == "Bear"
        assert "Write your entire response in Chinese" in captured["prompt"]
