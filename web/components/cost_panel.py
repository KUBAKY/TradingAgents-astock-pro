"""AI 成本面板 — 侧栏共用组件：今日/本周/本月汇总 + feature 分组 + 价格表状态 + 手动刷新。

三页共用（app.py / 1_短线分析 / 2_持仓管理）：render_cost_panel()
"""

from __future__ import annotations

from datetime import datetime

import streamlit as st

from tradingagents.cost import ledger, pricing

_FEATURE_LABELS = {
    "main_graph": "深度分析",
    "shortterm": "短线决策",
    "screener": "全市场选股",
    "portfolio": "持仓跟进",
}


def _fmt_ts(iso: str | None) -> str:
    if not iso:
        return "从未"
    try:
        return datetime.fromisoformat(iso).strftime("%m-%d %H:%M")
    except ValueError:
        return iso


def render_cost_panel() -> None:
    """侧栏成本汇总块（无参数，幂等）。"""
    st.markdown("#### 💰 AI 成本（人民币）")

    total_today = ledger.summarize("today")
    total_week = ledger.summarize("week")
    total_month = ledger.summarize("month")
    st.markdown(
        f"**今日** ¥{total_today['total_cost_cny']:.4f}（{total_today['calls']} 次）  \n"
        f"**本周** ¥{total_week['total_cost_cny']:.4f}（{total_week['calls']} 次）  \n"
        f"**本月** ¥{total_month['total_cost_cny']:.4f}（{total_month['calls']} 次）"
    )

    rows = total_week["rows"]
    if rows:
        lines = "\n".join(
            f"| {_FEATURE_LABELS.get(r['feature'], r['feature'])} | "
            f"¥{r['cost_cny']:.4f} | {r['calls']} |"
            for r in rows
        )
        st.markdown(
            f"**本周分组**\n\n| 场景 | 成本 | 调用 |\n|---|---|---|\n{lines}"
        )

    info = pricing.pricing_info()
    stale = bool(info.get("stale"))
    src = info.get("source") or "内置默认表"
    st.caption(
        f"价格表：{_fmt_ts(info.get('fetched_at'))}"
        + (f"（{src.split('/')[-1]}）" if "/" in src else f"（{src}）")
        + (" · ⚠️ 已过期" if stale else " · 有效")
    )
    if st.button("刷新定价", key="cost_refresh_pricing", use_container_width=True):
        try:
            pricing.refresh_pricing()
            st.success("✅ 定价已刷新")
        except Exception as e:
            st.error(f"❌ 刷新失败（已回退旧缓存）：{e}")
        st.rerun()


def render_run_cost(cost: dict | None) -> None:
    """单次运行成本徽章（决策卡头部/结果区用）。cost 为 pipeline.run 返回值里的 cost。"""
    if not cost:
        return
    calls = cost.get("calls", 0)
    total = cost.get("total_cost_cny")
    if total is None:
        st.caption("本次调用：未定价（模型不在价格表）")
    else:
        st.caption(f"💸 本次调用 {calls} 次 · ¥{total:.4f}")
