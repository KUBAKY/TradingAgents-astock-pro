"""决策渲染中文化测试：render 输出中文标题 + parse_rating 回环解析。"""

from tradingagents.agents.schemas import (
    PortfolioDecision,
    PortfolioRating,
    ResearchPlan,
    TraderAction,
    TraderProposal,
    render_pm_decision,
    render_research_plan,
    render_trader_proposal,
)
from tradingagents.agents.utils.rating import parse_rating


class TestPmDecisionRender:
    def _decision(self, rating):
        return PortfolioDecision(
            rating=rating,
            executive_summary="摘要内容。",
            investment_thesis="投资论点内容。",
            time_horizon="3-6个月",
        )

    def test_chinese_headings(self):
        out = render_pm_decision(self._decision(PortfolioRating.OVERWEIGHT))
        assert out.startswith("**评级**: 增持 (Overweight)")
        assert "**摘要**: 摘要内容。" in out
        assert "**投资论点**: 投资论点内容。" in out
        assert "**期限**: 3-6个月" in out
        assert "Executive Summary" not in out

    def test_rating_roundtrip_all_tiers(self):
        cases = [
            (PortfolioRating.BUY, "Buy", "买入"),
            (PortfolioRating.OVERWEIGHT, "Overweight", "增持"),
            (PortfolioRating.HOLD, "Hold", "持有"),
            (PortfolioRating.UNDERWEIGHT, "Underweight", "减持"),
            (PortfolioRating.SELL, "Sell", "卖出"),
        ]
        for rating, canonical, cn in cases:
            out = render_pm_decision(self._decision(rating))
            assert f"**评级**: {cn} ({canonical})" in out
            assert parse_rating(out) == canonical

    def test_no_time_horizon_omitted(self):
        d = PortfolioDecision(
            rating=PortfolioRating.HOLD,
            executive_summary="s",
            investment_thesis="t",
        )
        assert "**期限**" not in render_pm_decision(d)


class TestResearchPlanRender:
    def test_chinese_headings_and_roundtrip(self):
        plan = ResearchPlan(
            recommendation=PortfolioRating.UNDERWEIGHT,
            rationale="多方空方各执一词。",
            strategic_actions="分两批减仓。",
        )
        out = render_research_plan(plan)
        assert "**评级**: 减持 (Underweight)" in out
        assert "**理由**: 多方空方各执一词。" in out
        assert "**行动要点**: 分两批减仓。" in out
        assert parse_rating(out) == "Underweight"


class TestTraderProposalRender:
    def test_chinese_headings_and_stop_line(self):
        p = TraderProposal(action=TraderAction.BUY, reasoning="基本面强劲。")
        out = render_trader_proposal(p)
        assert "**操作**: 买入 (Buy)" in out
        assert "**理由**: 基本面强劲。" in out
        # 停止信号契约行保持英文原样
        assert "FINAL TRANSACTION PROPOSAL: **BUY**" in out

    def test_all_actions_roundtrip(self):
        for action, cn in [(TraderAction.BUY, "买入"),
                           (TraderAction.HOLD, "持有"),
                           (TraderAction.SELL, "卖出")]:
            out = render_trader_proposal(
                TraderProposal(action=action, reasoning="r"))
            assert f"**操作**: {cn} ({action.value})" in out
            assert parse_rating(out) == action.value


class TestDebaterChineseAdaptation:
    """风控三辩手：prompt 含中文输出指令，输出前缀中文化。"""

    def _state(self):
        return {
            "risk_debate_state": {
                "history": "", "aggressive_history": "",
                "conservative_history": "", "neutral_history": "",
                "current_conservative_response": "",
                "current_neutral_response": "",
                "current_aggressive_response": "",
                "count": 0,
            },
            "market_report": "m", "sentiment_report": "s",
            "news_report": "n", "fundamentals_report": "f",
            "policy_report": "p", "hot_money_report": "h",
            "lockup_report": "l",
            "trader_investment_plan": "t",
        }

    def _run(self, create_fn, monkeypatch, captured):
        from tradingagents.dataflows.config import set_config
        set_config({"output_language": "Chinese"})

        class _LLM:
            def invoke(self, prompt):
                captured["prompt"] = prompt
                return type("R", (), {"content": "中文论点"})()

        node = create_fn(_LLM())
        return node(self._state())

    def test_aggressive(self):
        from tradingagents.agents.risk_mgmt.aggressive_debator import (
            create_aggressive_debator,
        )
        captured = {}
        out = self._run(create_aggressive_debator, None, captured)
        assert "Write your entire response in Chinese" in captured["prompt"]
        assert out["risk_debate_state"]["current_aggressive_response"].startswith("激进分析师:")

    def test_conservative(self):
        from tradingagents.agents.risk_mgmt.conservative_debator import (
            create_conservative_debator,
        )
        captured = {}
        out = self._run(create_conservative_debator, None, captured)
        assert "Write your entire response in Chinese" in captured["prompt"]
        assert out["risk_debate_state"]["current_conservative_response"].startswith("保守分析师:")

    def test_neutral(self):
        from tradingagents.agents.risk_mgmt.neutral_debator import (
            create_neutral_debator,
        )
        captured = {}
        out = self._run(create_neutral_debator, None, captured)
        assert "Write your entire response in Chinese" in captured["prompt"]
        assert out["risk_debate_state"]["current_neutral_response"].startswith("中性分析师:")
