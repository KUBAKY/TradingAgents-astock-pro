"""执行轨迹浏览器：列表 + 单条详情（Prompt 全文 / 原始返回 / 重试对比）。"""

from __future__ import annotations

from datetime import datetime

import streamlit as st

from tradingagents.shortterm.trace import list_traces, load_trace


def render_trace_viewer() -> None:
    items = list_traces()
    if not items:
        st.caption("暂无执行轨迹（分析时勾选「记录执行轨迹」或设 ST_TRACE=1 后落盘）")
        return

    def _label(it):
        t = datetime.fromtimestamp(it["ts"]).strftime("%m-%d %H:%M")
        ok = it.get("ok")
        badge = "🟢" if ok else ("🔴" if ok is False else "⚪")
        retry = " ↻2" if it.get("attempts", 1) > 1 else ""
        return f"{badge}{retry} {it['trade_date']} {it.get('name','')}({it['ticker']}) {t}"

    options = {_label(it): it["path"] for it in items}
    sel = st.selectbox("选择轨迹", list(options.keys()), key="trace_sel")
    rec = load_trace(options[sel])

    m = st.columns(5)
    m[0].metric("模式", rec.get("mode", "—"))
    m[1].metric("模型", rec.get("model", "—"))
    m[2].metric("耗时", f"{rec['elapsed_ms']}ms" if rec.get("elapsed_ms") is not None else "—")
    m[3].metric("调用次数", rec.get("attempts", 1))
    val = rec.get("validation") or {}
    m[4].metric("校验", "通过" if val.get("ok") else "未通过")

    if not val.get("ok") and val.get("violations"):
        st.warning("；".join(val["violations"]))
    if val.get("unanchored"):
        st.caption("⚠️ 未锚定: " + "；".join(val["unanchored"]))

    if rec.get("attempts", 1) > 1 and rec.get("first_response"):
        with st.expander("首次输出（被拦截）+ 违规清单", expanded=False):
            if rec.get("first_violations"):
                st.error("；".join(rec["first_violations"]))
            st.markdown(rec["first_response"])

    with st.expander("LLM 原始返回（最终）", expanded=True):
        st.markdown(rec.get("response", ""))
    with st.expander("Prompt 全文"):
        st.text(rec.get("prompt", ""))
