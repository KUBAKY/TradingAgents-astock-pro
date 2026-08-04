"""数值锚定校验：报告引用的基本面数字必须能在数据源中找到（防幻觉软告警）。

LLM 可能编造"ROE 18.5%"这类精确数字并据此推理。此处抽取报告中的
基本面指标引用（ROE/PE/营收/净利润等），与本次实际获取的数据原文比对，
找不到对应数值则记为"未锚定"——软告警，不阻塞、不触发重试
（交易计划里的目标价/止损位属推演值，不参与校验）。
"""

from __future__ import annotations

import re

# 指标关键词（引用这些指标的数字才需要锚定）
_ANCHOR_KEYWORDS = (
    "ROE", "ROA", "PE", "PB", "PS", "EPS",
    "市盈率", "市净率", "营收", "营业收入", "净利润", "毛利", "毛利率",
    "净利率", "总市值", "流通市值", "市值", "换手率", "股息率",
)

_MENTION_RE = re.compile(
    r"(ROE|ROA|PE|PB|PS|EPS|市盈率|市净率|营收|营业收入|净利润|毛利率?|"
    r"净利率|总市值|流通市值|市值|换手率|股息率)"
    r"[^\d%]{0,8}?([-+]?\d+(?:\.\d+)?)",
    re.IGNORECASE,
)

_NUM_RE = re.compile(r"[-+]?\d+(?:\.\d+)?")

# 推演值行（目标价/止损位等交易计划条目），锚定检查前剔除
_PLAN_LINE_RE = re.compile(
    r"^\s*[-*]?\s*(介入条件|目标价|预期周期|仓位建议|止损位|失效条件|次日观察点)",
    re.MULTILINE,
)


def _source_numbers(source: str) -> list[float]:
    return [float(m.group()) for m in _NUM_RE.finditer(source or "")]


def _is_anchored(value: float, source_nums: list[float]) -> bool:
    """±1%（或 ±0.05 绝对值）容差内存在同源数值 → 视为锚定（容忍四舍五入）。"""
    tol = max(0.05, abs(value) * 0.01)
    return any(abs(s - value) <= tol for s in source_nums)


def check_numeric_anchors(report: str, source: str) -> list[str]:
    """返回未锚定告警清单（空 = 全部锚定或无基本面引用）。"""
    text = _PLAN_LINE_RE.sub("", report or "")
    nums = _source_numbers(source)
    if not nums:
        return []

    violations: list[str] = []
    seen: set[tuple[str, str]] = set()
    for m in _MENTION_RE.finditer(text):
        keyword, raw = m.group(1), m.group(2)
        key = (keyword.upper(), raw)
        if key in seen:
            continue
        seen.add(key)
        if not _is_anchored(float(raw), nums):
            violations.append(f"未锚定数值: {keyword} {raw}（数据源中未找到）")
    return violations
