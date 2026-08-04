"""短线决策卡输出契约校验（schema 强制，参考 TradingAgents-CN v3 思路）。

LLM 是概率模型，prompt 约束格式不稳定。此处代码级校验决策卡必备字段/枚举值/
必备章节，返回违规清单；pipeline 据此做反馈重试，防止格式漂移悄悄流入下游
（历史落盘解析、事后评估、UI 展示均依赖固定格式）。
"""

from __future__ import annotations

import re

CARD_MODES = ("ultra_short", "swing")

_DIRECTIONS = ("买入", "观望", "卖出", "回避")
_CONFIDENCES = ("高", "中", "低")
_HORIZONS = ("隔日超短", "3-10日波段")

_REQUIRED_SECTIONS = ("### 逻辑链", "### 交易计划", "### 机构行为反向推理")
_REQUIRED_PLAN_ITEMS = ("介入条件", "目标价", "预期周期", "仓位建议",
                        "止损位", "失效条件", "次日观察点")

_FIELD_RE = {
    "方向": (re.compile(r"\*\*方向\*\*[:：]\s*([^\n*]+)"), _DIRECTIONS),
    "置信度": (re.compile(r"\*\*置信度\*\*[:：]\s*([^\n*]+)"), _CONFIDENCES),
    "适用周期": (re.compile(r"\*\*适用周期\*\*[:：]\s*([^\n*]+)"), _HORIZONS),
}


def validate_decision_card(report: str, mode: str) -> list[str]:
    """校验决策卡格式，返回违规清单（空 = 通过）。

    非决策卡模式（quick/blacklist/ch0_only）无格式要求，直接放行。
    """
    if mode not in CARD_MODES:
        return []

    text = report or ""
    violations: list[str] = []

    for field, (pattern, allowed) in _FIELD_RE.items():
        m = pattern.search(text)
        if not m:
            violations.append(f"缺少 **{field}** 字段")
            continue
        value = m.group(1).strip()
        if not any(value.startswith(a) for a in allowed):
            violations.append(
                f"{field}值非法: {value}（允许: {'/'.join(allowed)}）")

    for section in _REQUIRED_SECTIONS:
        if section not in text:
            violations.append(f"缺少章节 {section}")

    for item in _REQUIRED_PLAN_ITEMS:
        if item not in text:
            violations.append(f"交易计划缺少条目: {item}")

    return violations


def build_retry_feedback(report: str, violations: list[str]) -> str:
    """构造反馈重试 prompt 片段：违规清单 + 上次输出，要求修正后完整重出。"""
    lines = [
        "",
        "",
        "## 格式校验未通过（必须修正后重新输出完整决策卡）",
        "",
        "你上次输出的违规项：",
    ]
    lines += [f"- {v}" for v in violations]
    lines += [
        "",
        "你上次的输出：",
        report,
        "",
        "请只输出修正后的完整决策卡，严格遵守输出格式，不要解释。",
    ]
    return "\n".join(lines)
