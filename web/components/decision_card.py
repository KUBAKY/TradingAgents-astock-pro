"""决策卡结构化头部：方向大字卡 + 置信度/周期徽章 + 校验状态。

方向配色按 A 股惯例：买入红 / 卖出绿 / 观望灰 / 回避黄。
"""

from __future__ import annotations

import streamlit as st

from tradingagents.shortterm.history import parse_decision

_STYLES = {
    "买入": {"color": "#ff4b4b", "icon": "▲"},
    "卖出": {"color": "#21c354", "icon": "▼"},
    "观望": {"color": "#888888", "icon": "◆"},
    "回避": {"color": "#ffa421", "icon": "⛔"},
}
_FALLBACK = {"color": "#888888", "icon": "?"}


def direction_style(direction: str | None) -> dict:
    """方向 → 展示样式（颜色/图标），测试锚点。"""
    return _STYLES.get(direction or "", _FALLBACK)


def render_decision_header(report: str, validation: dict | None = None) -> None:
    """报告上方渲染结构化头部；未解析出方向时静默跳过（quick 模式等）。"""
    parsed = parse_decision(report)
    direction = parsed.get("direction")
    if not direction:
        return
    style = direction_style(direction)

    badges = []
    if parsed.get("confidence"):
        badges.append(f"置信度 {parsed['confidence']}")
    if parsed.get("horizon"):
        badges.append(parsed["horizon"])
    if validation:
        if validation.get("ok") and validation.get("retried"):
            badges.append("🟡 格式校验·重试修正")
        elif validation.get("ok"):
            badges.append("🟢 格式校验通过")
        else:
            badges.append("🔴 格式校验未通过")

    st.markdown(
        f"""<div style="
            display: flex; align-items: center; gap: 1.2rem;
            padding: 0.9rem 1.2rem; margin-bottom: 0.8rem;
            background: #111; border: 1px solid #222; border-radius: 10px;
        ">
            <span style="font-size: 1.8rem; font-weight: 900; color: {style['color']};">
                {style['icon']} {direction}
            </span>
            <span style="color: #888; font-size: 0.85rem;">
                {'&nbsp;·&nbsp;'.join(badges)}
            </span>
        </div>""",
        unsafe_allow_html=True,
    )

    if validation and not validation.get("ok"):
        st.warning("决策卡格式校验未通过（已重试）："
                   + "；".join(validation.get("violations", [])))
    if validation and validation.get("unanchored"):
        st.caption("⚠️ 以下数字未在数据源中找到（可能为模型推算或幻觉，请核实）："
                   + "；".join(validation["unanchored"]))
