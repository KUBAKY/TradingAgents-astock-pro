"""Pydantic schemas used by agents that produce structured output.

The framework's primary artifact is still prose: each agent's natural-language
reasoning is what users read in the saved markdown reports and what the
downstream agents read as context.  Structured output is layered onto the
three decision-making agents (Research Manager, Trader, Portfolio Manager)
so that:

- Their outputs follow consistent section headers across runs and providers
- Each provider's native structured-output mode is used (json_schema for
  OpenAI/xAI, response_schema for Gemini, tool-use for Anthropic)
- Schema field descriptions become the model's output instructions, freeing
  the prompt body to focus on context and the rating-scale guidance
- A render helper turns the parsed Pydantic instance back into the same
  markdown shape the rest of the system already consumes, so display,
  memory log, and saved reports keep working unchanged
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Shared rating types
# ---------------------------------------------------------------------------


class PortfolioRating(str, Enum):
    """5-tier rating used by the Research Manager and Portfolio Manager."""

    BUY = "Buy"
    OVERWEIGHT = "Overweight"
    HOLD = "Hold"
    UNDERWEIGHT = "Underweight"
    SELL = "Sell"


# 评级中英映射：渲染层做 A 股中文适配（"增持 (Overweight)" 双格式）。
# 兼容性：parse_rating 第 4 通道（中文评级词表）命中行首中文值，
# 第 3 通道（英文裸词）兜底，两条路径都回到同一 canonical rating。
RATING_CN = {
    "Buy": "买入",
    "Overweight": "增持",
    "Hold": "持有",
    "Underweight": "减持",
    "Sell": "卖出",
}


def _dual_rating(value: str) -> str:
    """评级双格式 '增持 (Overweight)'：中文展示，parse_rating 中英文通道均可识别。"""
    return f"{RATING_CN.get(value, value)} ({value})"


class TraderAction(str, Enum):
    """3-tier transaction direction used by the Trader.

    The Trader's job is to translate the Research Manager's investment plan
    into a concrete transaction proposal: should the desk execute a Buy, a
    Sell, or sit on Hold this round.  Position sizing and the nuanced
    Overweight / Underweight calls happen later at the Portfolio Manager.
    """

    BUY = "Buy"
    HOLD = "Hold"
    SELL = "Sell"


# ---------------------------------------------------------------------------
# Research Manager
# ---------------------------------------------------------------------------


class ResearchPlan(BaseModel):
    """Structured investment plan produced by the Research Manager.

    Hand-off to the Trader: the recommendation pins the directional view,
    the rationale captures which side of the bull/bear debate carried the
    argument, and the strategic actions translate that into concrete
    instructions the trader can execute against.
    """

    recommendation: PortfolioRating = Field(
        description=(
            "The investment recommendation. Exactly one of Buy / Overweight / "
            "Hold / Underweight / Sell. Reserve Hold for situations where the "
            "evidence on both sides is genuinely balanced; otherwise commit to "
            "the side with the stronger arguments."
        ),
    )
    rationale: str = Field(
        description=(
            "Conversational summary of the key points from both sides of the "
            "debate, ending with which arguments led to the recommendation. "
            "Speak naturally, as if to a teammate."
        ),
    )
    strategic_actions: str = Field(
        description=(
            "Concrete steps for the trader to implement the recommendation, "
            "consistent with the rating."
        ),
    )


def render_research_plan(plan: ResearchPlan) -> str:
    """Render a ResearchPlan to markdown for storage and the trader's prompt context.

    中文标题 + 双格式评级（见 ``_dual_rating``），下游 parse_rating 不受影响。
    """
    return "\n".join([
        f"**评级**: {_dual_rating(plan.recommendation.value)}",
        "",
        f"**理由**: {plan.rationale}",
        "",
        f"**行动要点**: {plan.strategic_actions}",
    ])


# ---------------------------------------------------------------------------
# Trader
# ---------------------------------------------------------------------------


class TraderProposal(BaseModel):
    """Structured transaction proposal produced by the Trader.

    The trader reads the Research Manager's investment plan and the analyst
    reports, then states a direction and the reasoning behind it.

    It deliberately carries **no executable price levels** — no entry price,
    no stop-loss, no position size. This project is a research and education
    implementation of the upstream TradingAgents framework, and concrete trade
    levels for a named security are what turn a research tool into an
    investment-advisory product. The capability is not shipped here; a
    downstream fork that wants it can add it under its own responsibility.
    """

    action: TraderAction = Field(
        description="The transaction direction. Exactly one of Buy / Hold / Sell.",
    )
    reasoning: str = Field(
        description=(
            "The case for this action, anchored in the analysts' reports and "
            "the research plan. Two to four sentences. Do not quote specific "
            "entry, stop-loss or position-size levels."
        ),
    )


def render_trader_proposal(proposal: TraderProposal) -> str:
    """Render a TraderProposal to markdown.

    中文标题 + 双格式操作方向；末尾 ``FINAL TRANSACTION PROPOSAL: **BUY/HOLD/SELL**``
    行保持英文原样——它是分析师停止信号的文本契约，任何外部代码 grep 它。
    """
    return "\n".join([
        f"**操作**: {_dual_rating(proposal.action.value)}",
        "",
        f"**理由**: {proposal.reasoning}",
        "",
        f"FINAL TRANSACTION PROPOSAL: **{proposal.action.value.upper()}**",
    ])


# ---------------------------------------------------------------------------
# Portfolio Manager
# ---------------------------------------------------------------------------


class PortfolioDecision(BaseModel):
    """Structured output produced by the Portfolio Manager.

    The model fills every field as part of its primary LLM call; no separate
    extraction pass is required. Field descriptions double as the model's
    output instructions, so the prompt body only needs to convey context and
    the rating-scale guidance.

    Like :class:`TraderProposal`, this carries no price target and no other
    executable level — see that class for why.
    """

    rating: PortfolioRating = Field(
        description=(
            "The final position rating. Exactly one of Buy / Overweight / Hold / "
            "Underweight / Sell, picked based on the analysts' debate."
        ),
    )
    executive_summary: str = Field(
        description=(
            "A concise summary of what drove the rating and the main "
            "considerations on each side. Two to four sentences. Do not quote "
            "specific entry, stop-loss, position-size or target-price levels."
        ),
    )
    investment_thesis: str = Field(
        description=(
            "Detailed reasoning anchored in specific evidence from the analysts' "
            "debate. If prior lessons are referenced in the prompt context, "
            "incorporate them; otherwise rely solely on the current analysis."
        ),
    )
    time_horizon: Optional[str] = Field(
        default=None,
        description="Optional analysis horizon, e.g. '3-6 months'.",
    )


def render_pm_decision(decision: PortfolioDecision) -> str:
    """Render a PortfolioDecision back to markdown for the rest of the system.

    中文标题 + 双格式评级（``**评级**: 增持 (Overweight)``，见 ``_dual_rating``）：
    UI/报告中文可读，parse_rating 经中文评级词（第 4 通道）或英文裸词
    （第 3 通道）均可提取 canonical rating，memory log / CLI / 信号处理不受影响。
    """
    parts = [
        f"**评级**: {_dual_rating(decision.rating.value)}",
        "",
        f"**摘要**: {decision.executive_summary}",
        "",
        f"**投资论点**: {decision.investment_thesis}",
    ]
    if decision.time_horizon:
        parts.extend(["", f"**期限**: {decision.time_horizon}"])
    return "\n".join(parts)
