"""短线交易分析 — 个股决策 / 全市场选股（tradingagents.shortterm 的 Web 封装）。"""

from __future__ import annotations

import json
import os
import sys
from datetime import date
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

load_dotenv(_PROJECT_ROOT / ".env", override=True)

from tradingagents.shortterm import pipeline, screener  # noqa: E402
from tradingagents.shortterm.history import (  # noqa: E402
    aggregate_stats,
    evaluate_call,
    list_records,
    load_record,
    save_screen_record,
    save_stock_record,
)
from web.shortterm_jobs import clear_job, get_job, latest_job_id, start_job  # noqa: E402

st.set_page_config(page_title="短线分析", page_icon="⚡", layout="wide")

st.markdown("""<style>
.stApp { background: #0a0a0a; }
button[kind="primary"] {
    background: linear-gradient(135deg, #ff5a1f, #ff8c42) !important;
    border: none !important; font-weight: 700 !important;
}
</style>""", unsafe_allow_html=True)

st.title("⚡ A股短线分析")

_ENV_KEYS = {
    "anthropic": "ANTHROPIC_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "minimax": "MINIMAX_API_KEY",
    "qwen": "DASHSCOPE_API_KEY",
    "glm": "ZHIPU_API_KEY",
    "openai_compatible": "OPENAI_COMPATIBLE_API_KEY",
}


from web.llm_keys import get_pref, render_api_key_input, set_pref  # noqa: E402

_PROVIDERS_ST = ["anthropic", "deepseek", "minimax", "qwen", "glm", "openai_compatible"]
_DEFAULT_MODELS = {
    "anthropic": "claude-haiku-4-5", "deepseek": "deepseek-chat",
    "minimax": "MiniMax-M2.7", "qwen": "qwen-plus", "glm": "glm-4",
    "openai_compatible": "",
}

if "st_provider_idx" not in st.session_state:
    saved_p = get_pref("ST_PROVIDER", "anthropic")
    st.session_state["st_provider_idx"] = _PROVIDERS_ST.index(saved_p) if saved_p in _PROVIDERS_ST else 0
if "st_model" not in st.session_state:
    st.session_state["st_model"] = get_pref("ST_MODEL")
if "st_base_url" not in st.session_state:
    st.session_state["st_base_url"] = get_pref("ST_BASE_URL")

with st.sidebar:
    st.header("LLM 设置")

    def _on_provider_change():
        p = _PROVIDERS_ST[st.session_state["st_provider_idx"]]
        set_pref("ST_PROVIDER", p)
        st.session_state["st_model"] = _DEFAULT_MODELS.get(p, "")
        set_pref("ST_MODEL", st.session_state["st_model"])

    provider_idx = st.selectbox(
        "Provider", range(len(_PROVIDERS_ST)),
        format_func=lambda i: _PROVIDERS_ST[i],
        key="st_provider_idx",
        on_change=_on_provider_change,
    )
    provider = _PROVIDERS_ST[provider_idx]

    model = st.text_input(
        "Model", key="st_model",
        placeholder=_DEFAULT_MODELS.get(provider, ""),
        on_change=lambda: set_pref("ST_MODEL", st.session_state["st_model"]),
    ) or _DEFAULT_MODELS.get(provider, "")
    base_url = st.text_input(
        "Base URL（可选）", key="st_base_url",
        on_change=lambda: set_pref("ST_BASE_URL", st.session_state["st_base_url"]),
    ) or None

    render_api_key_input(provider, "stpage")

    st.caption(f"短线决策单节点：**{provider} · {model}**"
               + (f"（{base_url}）" if base_url else ""))
    if provider == "anthropic":
        st.caption("anthropic 走 requests 直连（绕开 headroom-proxy 的 httpx 502）")

tab_stock, tab_screen, tab_hist = st.tabs(["个股决策", "全市场选股", "历史复盘"])


def show_result(result: dict):
    ch0 = result["ch0"]
    if result["mode"] == "blacklist":
        st.error(result["report"])
        return
    m = ch0.get("metrics", {})
    cols = st.columns(5)
    cols[0].metric("收盘", m.get("last_close"))
    cols[1].metric("7日涨跌", f"{m.get('ret_7d_pct')}%")
    cols[2].metric("30日涨跌", f"{m.get('ret_30d_pct')}%")
    cols[3].metric("量比", m.get("vol_ratio_vs_5d"))
    cols[4].metric("模式", ch0["mode_hint"]["label"])
    if ch0.get("anomalies"):
        st.warning(" | ".join(a["signal"] for a in ch0["anomalies"]))
    st.markdown(result["report"])
    with st.expander("Ch0 扫描原始数据"):
        st.json(ch0)
    if result.get("bundle"):
        with st.expander("数据包原文（喂给 LLM 的原始数据）"):
            st.text(result["bundle"])
    st.download_button("下载报告 (md)", result["report"],
                       file_name=f"{ch0['ticker']}_{ch0['trade_date']}_shortterm.md")


with tab_stock:
    c1, c2, c3 = st.columns(3)
    ticker = c1.text_input("股票代码", value="", placeholder="如 000725")
    trade_date = c2.date_input("分析日期", value=date.today()).strftime("%Y-%m-%d")
    intent = c3.text_input("你的诉求（可选）", key="intent_input",
                           placeholder="如：连板想追 / 被套要不要割")

    def _set_intent(txt):
        st.session_state["intent_input"] = txt

    chips = st.columns(4)
    chips[0].button("连板想追", key="chip_chase", on_click=_set_intent,
                    args=("连板想追高，评估能否介入、什么条件下介入",))
    chips[1].button("被套要不要割", key="chip_stuck", on_click=_set_intent,
                    args=("已持仓被套，明确回答割/持/补三选一及理由",))
    chips[2].button("打板候选", key="chip_board", on_click=_set_intent,
                    args=("准备打板介入，评估次日溢价概率与风险",))
    chips[3].button("低吸机会", key="chip_dip", on_click=_set_intent,
                    args=("想找回踩低吸点，评估关键支撑位与介入信号",))

    c4, c5, c6 = st.columns(3)
    capital = c4.number_input("总资金（元）", min_value=0, value=0, step=10000) or None
    cost = c5.number_input("持仓成本价（被套分析用）", min_value=0.0, value=0.0, step=0.01) or None
    shares = c6.number_input("持仓股数", min_value=0, value=0, step=100) or None

    ch0_only = st.checkbox("只跑 Ch0 扫描（不调 LLM，免费）", value=False)

    if st.button("开始分析", type="primary", key="stock_run"):
        if not ticker.strip():
            st.error("请输入股票代码")
        else:
            def _stock():
                res = pipeline.run(
                    ticker.strip(), trade_date, intent, capital, cost, shares,
                    provider, model or "claude-haiku-4-5", base_url, ch0_only,
                )
                save_stock_record(res, {"intent": intent, "capital": capital,
                                        "cost": cost, "shares": shares})
                return res

            job_id = start_job("stock", _stock)
            st.session_state["stock_job"] = job_id
            st.session_state.pop("st_result", None)

    job = get_job(st.session_state.get("stock_job", "")) or get_job(latest_job_id("stock") or "") or {}
    status = job.get("status")
    if status in ("queued", "running"):
        # 仅运行期挂载轮询 fragment：完成后 app 级 rerun 落回静态分支，空闲零 churn
        @st.fragment(run_every=2)
        def _poll_stock():
            j = get_job(st.session_state.get("stock_job", "")) or get_job(latest_job_id("stock") or "") or {}
            if j.get("status") in ("queued", "running"):
                st.info("分析中（Ch0扫描 → 数据包 → LLM决策）… 可切换页面，任务不中断")
            else:
                st.rerun()

        _poll_stock()
    elif status == "error":
        st.error(f"分析失败: {job['error']}")
        if st.button("清除", key="stock_clear"):
            clear_job(st.session_state.pop("stock_job", latest_job_id("stock") or ""))
            st.rerun()
    elif status == "done":
        show_result(job["result"])
    elif st.session_state.get("st_result"):
        show_result(st.session_state["st_result"])

with tab_screen:
    s1, s2 = st.columns(2)
    s_capital = s1.number_input("可用资金（元）", min_value=0, value=200000, step=10000, key="s_cap") or None
    per_board = s2.slider("每板块精扫候选数", 3, 15, 8)
    s_no_llm = st.checkbox("只输出扫描JSON（不调LLM）", value=False, key="s_nollm")

    if st.button("开始扫描", type="primary", key="screen_run"):
        def _screen():
            scan_result = screener.scan(s_capital, per_board)
            if s_no_llm:
                save_screen_record(scan_result, None)
                return ("json", scan_result)
            text = screener.recommend(scan_result, provider, model or "claude-haiku-4-5", base_url)
            save_screen_record(scan_result, text)
            return ("report", text, scan_result)

        job_id = start_job("screen", _screen)
        st.session_state["screen_job"] = job_id
        st.session_state.pop("sc_result", None)

    def _render_screen_result(sc):
        if sc[0] == "json":
            st.json(sc[1])
        else:
            st.markdown(sc[1])
            with st.expander("候选池原始数据"):
                st.json(sc[2])
            st.download_button("下载推荐 (md)", sc[1], file_name="screener.md")

    sjob = get_job(st.session_state.get("screen_job", "")) or get_job(latest_job_id("screen") or "") or {}
    sstatus = sjob.get("status")
    if sstatus in ("queued", "running"):
        # 仅运行期挂载轮询 fragment：完成后 app 级 rerun 落回静态分支，空闲零 churn
        @st.fragment(run_every=3)
        def _poll_screen():
            j = get_job(st.session_state.get("screen_job", "")) or get_job(latest_job_id("screen") or "") or {}
            if j.get("status") in ("queued", "running"):
                st.info("全市场扫描中（约1分钟）… 可切换页面，任务不中断")
            else:
                st.rerun()

        _poll_screen()
    elif sstatus == "error":
        st.error(f"扫描失败: {sjob['error']}")
        if st.button("清除", key="screen_clear"):
            clear_job(st.session_state.pop("screen_job", latest_job_id("screen") or ""))
            st.rerun()
    elif sstatus == "done":
        _render_screen_result(sjob["result"])
    elif st.session_state.get("sc_result"):
        _render_screen_result(st.session_state["sc_result"])

with tab_hist:
    records = list_records()
    if not records:
        st.caption("暂无短线历史记录（个股决策/选股完成后自动落盘）")
    else:
        from datetime import datetime as _dt

        st.subheader("📊 复盘胜率")
        with st.spinner("聚合全部已评分决策…"):
            stats = aggregate_stats(records)
        if stats["scored"]:
            m = st.columns(6)
            m[0].metric("已评分决策", stats["scored"])
            m[1].metric("对", stats["wins"], delta_color="off")
            m[2].metric("错", stats["losses"], delta_color="off")
            m[3].metric("胜率", f"{stats['win_rate']:.1f}%")
            m[4].metric("平均 T+3", f"{stats['avg_t3_close']}%")
            m[5].metric("平均 T+10", f"{stats['avg_t10_close']}%" if stats["avg_t10_close"] is not None else "—")
            d1, d2 = st.columns(2)
            d1.metric("买入胜率", f"{stats['by_direction']['买入']['win_rate']:.1f}%"
                      if stats["by_direction"]["买入"]["count"] else "—",
                      help=f"买入 {stats['by_direction']['买入']['wins']}胜/"
                           f"{stats['by_direction']['买入']['losses']}负 · "
                           f"平均T+3 {stats['by_direction']['买入']['avg_t3']}%")
            d2.metric("卖出胜率", f"{stats['by_direction']['卖出']['win_rate']:.1f}%"
                      if stats["by_direction"]["卖出"]["count"] else "—",
                      help=f"卖出 {stats['by_direction']['卖出']['wins']}胜/"
                           f"{stats['by_direction']['卖出']['losses']}负 · "
                           f"平均T+3 {stats['by_direction']['卖出']['avg_t3']}%")
            if stats["best"] or stats["worst"]:
                b, w = st.columns(2)
                if stats["best"]:
                    b.caption(f"🏆 最优: {stats['best']['name']}({stats['best']['ticker']}) "
                              f"{stats['best']['trade_date']} {stats['best']['direction']} "
                              f"T+3 {stats['best']['t3']:+.2f}%")
                if stats["worst"]:
                    w.caption(f"📉 最差: {stats['worst']['name']}({stats['worst']['ticker']}) "
                              f"{stats['worst']['trade_date']} {stats['worst']['direction']} "
                              f"T+3 {stats['worst']['t3']:+.2f}%")
            if stats["recent"]:
                st.markdown("**最近判定**")
                st.dataframe(
                    [{"日期": x["trade_date"], "标的": f"{x['name']}({x['ticker']})",
                      "方向": x["direction"], "T+3": f"{x['t3']:+.2f}%", "判定": x["verdict"]}
                     for x in stats["recent"]],
                    use_container_width=True, hide_index=True,
                )
            if stats["pending"]:
                st.caption(f"另 {stats['pending']} 条待验证/不评分（观望、回避或K线不足）")
        else:
            st.caption("暂无已评分决策（需方向=买入/卖出且后续 ≥3 根K线）")

        st.subheader("记录明细")

        def _label(r):
            t = _dt.fromtimestamp(r["ts"]).strftime("%m-%d %H:%M")
            if r["kind"] == "screen":
                return f"[选股] {r['trade_date']} {t}"
            d = (r.get("parsed") or {}).get("direction") or r.get("mode") or "?"
            return f"[{d}] {r['trade_date']} {r.get('name','')}({r['ticker']}) {t}"

        options = {_label(r): r["path"] for r in records}
        sel = st.selectbox("选择记录", list(options.keys()))
        rec = load_record(options[sel])

        if rec["kind"] == "screen":
            if rec.get("report"):
                st.markdown(rec["report"])
            with st.expander("扫描原始数据"):
                st.json(rec["scan"])
        else:
            parsed = rec.get("parsed") or {}
            with st.spinner("计算事后走势…"):
                try:
                    ev = evaluate_call(rec)
                except Exception as e:
                    ev = None
                    st.warning(f"事后评估失败: {e}")
            if ev:
                cols = st.columns(6)
                cols[0].metric("方向", parsed.get("direction") or "未解析")
                cols[1].metric("基准收盘", ev.get("entry_close"))
                cols[2].metric("T+1收盘", f"{ev['t1_close_pct']}%" if ev.get("t1_close_pct") is not None else "—")
                cols[3].metric("T+3收盘", f"{ev['t3_close_pct']}%" if ev.get("t3_close_pct") is not None else "—")
                cols[4].metric("T+10收盘", f"{ev['t10_close_pct']}%" if ev.get("t10_close_pct") is not None else "—")
                verdict = ev.get("verdict", "—")
                cols[5].metric("判定", verdict)
                if ev.get("verdict_basis"):
                    st.caption(f"判定依据: {ev['verdict_basis']}（后续K线 {ev.get('bars_after')} 根）")
                if ev.get("hit_first"):
                    h = st.columns(4)
                    h[0].metric("先触发", ev["hit_first"], delta=f"T+{ev.get('hit_bar')}")
                    h[1].metric("触发收益", f"{ev.get('hit_pct')}%" if ev.get("hit_pct") is not None else "—")
                    h[2].metric("止损位", f"{ev.get('stop_px')}" if ev.get("stop_px") is not None else "—")
                    h[3].metric("目标位", f"{ev.get('target_px')}" if ev.get("target_px") is not None else "—")
                elif ev.get("raw_stop") or ev.get("raw_target"):
                    h = st.columns(4)
                    h[0].metric("止损位", f"{ev.get('stop_px')}" if ev.get("stop_px") is not None else "—")
                    h[1].metric("目标位", f"{ev.get('target_px')}" if ev.get("target_px") is not None else "—")
                    h[2].metric("先触发", "未触达")
                    h[3].metric("触发收益", "—")
            st.markdown(rec.get("report", ""))
            with st.expander("Ch0 扫描原始数据"):
                st.json(rec["ch0"])
            if rec.get("bundle"):
                with st.expander("数据包原文"):
                    st.text(rec["bundle"])
