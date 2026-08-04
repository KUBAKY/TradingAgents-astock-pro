"""断言规则库：禁用术语/禁用模式（合规与质量红线，热插拔）。

内置规则兜底 A 股合规红线（禁止直接买卖建议/收益承诺类表述）。
用户可在 ~/.tradingagents/shortterm/assertions.json 扩展，无需改代码重启:

    {"banned_terms": ["满仓干"], "banned_patterns": ["收益保证"]}

规则来源：实际跑出来的失败案例人工提炼（参考 TradingAgents-CN v3 断言机制）。
命中即视为决策卡违规，由 pipeline 反馈重试。
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

_USER_RULES_PATH = Path.home() / ".tradingagents" / "shortterm" / "assertions.json"

# 内置合规红线（否定语境如"不建议买入"不触发）
_BUILTIN_TERMS = (
    "建议买入", "建议卖出", "建议满仓", "建议抄底",
    "稳赚不赔", "稳赚", "必涨", "必跌", "包赚",
    "保本", "收益保证", "保证收益", "无风险套利", "百分百",
)

_NEG_PREFIXES = ("不", "勿", "莫", "非", "切勿", "禁止", "避免")

_RULES_CACHE: dict = {}


def _load_rules() -> dict:
    """加载内置+用户规则，按文件 mtime 缓存（热插拔）。"""
    path = Path(_USER_RULES_PATH)
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = None
    cached = _RULES_CACHE.get("rules")
    if cached is not None and _RULES_CACHE.get("mtime") == mtime:
        return cached

    rules: dict = {"banned_terms": list(_BUILTIN_TERMS), "banned_patterns": []}
    if mtime is not None:
        try:
            with open(path, encoding="utf-8") as f:
                user = json.load(f)
            rules["banned_terms"] += [str(t) for t in user.get("banned_terms", [])]
            rules["banned_patterns"] += [str(p) for p in user.get("banned_patterns", [])]
        except (OSError, json.JSONDecodeError, AttributeError):
            pass  # 用户规则损坏不阻塞，内置规则兜底

    _RULES_CACHE["rules"] = rules
    _RULES_CACHE["mtime"] = mtime
    return rules


def check_assertions(report: str) -> list[str]:
    """检查报告是否命中断言规则，返回违规清单（空 = 通过）。"""
    text = report or ""
    rules = _load_rules()
    violations: list[str] = []

    for term in rules["banned_terms"]:
        start = 0
        while True:
            i = text.find(term, start)
            if i < 0:
                break
            prefix = text[max(0, i - 2):i]
            if not any(prefix.endswith(neg) for neg in _NEG_PREFIXES):
                violations.append(f"命中禁用术语: {term}")
                break
            start = i + len(term)

    for pattern in rules["banned_patterns"]:
        try:
            if re.search(pattern, text):
                violations.append(f"命中禁用模式: {pattern}")
        except re.error:
            continue

    return violations
