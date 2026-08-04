"""持仓管理 — 持仓 CRUD + 每日跟进（割/持/补）+ 快照盈亏（tradingagents.shortterm.portfolio 的 Web 封装）。"""

from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

load_dotenv(_PROJECT_ROOT / ".env", override=True)

from tradingagents.shortterm import portfolio  # noqa: E402
from web.components.cost_panel import render_cost_panel, render_run_cost  # noqa: E402
from web.shortterm_jobs import clear_job, get_job, latest_job_id, start_job  # noqa: E402
from web.ui_theme import apply_theme  # noqa: E402

st.set_page_config(page_title="持仓管理", page_icon="📊", layout="wide")

apply_theme()

st.title("📊 持仓管理")
st.caption("持仓增删改 · 每日盘后跟进（割/持/补决策） · 当日快照盈亏（launchd 自动 15:15 跟进）")

_ENV_KEYS = {
    "anthropic": "ANTHROPIC_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "minimax": "MINIMAX_API_KEY",
    "qwen": "DASHSCOPE_API_KEY",
    "glm": "ZHIPU_API_KEY",
    "openai_compatible": "OPENAI_COMPATIBLE_API_KEY",
}

from web.components.ticker_input import ticker_input  # noqa: E402
from web.llm_keys import get_pref, render_api_key_input, set_pref  # noqa: E402

_PROVIDERS_ST = ["anthropic", "deepseek", "minimax", "qwen", "glm", "openai_compatible"]
_DEFAULT_MODELS = {
    "anthropic": "claude-haiku-4-5", "deepseek": "deepseek-v4-flash",
    "minimax": "MiniMax-M2.7", "qwen": "qwen-plus", "glm": "glm-4",
    "openai_compatible": "",
}

if "pf_provider_idx" not in st.session_state:
    saved_p = get_pref("ST_PROVIDER", "anthropic")
    st.session_state["pf_provider_idx"] = _PROVIDERS_ST.index(saved_p) if saved_p in _PROVIDERS_ST else 0
if "pf_model" not in st.session_state:
    st.session_state["pf_model"] = get_pref("ST_MODEL")

with st.sidebar:
    st.header("LLM 设置")

    def _on_provider_change():
        p = _PROVIDERS_ST[st.session_state["pf_provider_idx"]]
        set_pref("ST_PROVIDER", p)
        st.session_state["pf_model"] = _DEFAULT_MODELS.get(p, "")
        set_pref("ST_MODEL", st.session_state["pf_model"])

    provider_idx = st.selectbox(
        "Provider", range(len(_PROVIDERS_ST)),
        index=st.session_state["pf_provider_idx"],
        key="pf_provider_idx",
        format_func=lambda i: _PROVIDERS_ST[i],
        on_change=_on_provider_change,
    )
    provider = _PROVIDERS_ST[provider_idx]
    model = st.text_input("Model", key="pf_model",
                          on_change=lambda: set_pref("ST_MODEL", st.session_state["pf_model"]))
    if not model:
        model = _DEFAULT_MODELS.get(provider, "")
    if _ENV_KEYS.get(provider):
        render_api_key_input(provider, _ENV_KEYS[provider])
    st.divider()
    render_cost_panel()

st.divider()
tab_list, tab_add, tab_follow, tab_snap = st.tabs(
    ["持仓列表", "添加持仓", "每日跟进", "历史快照"])

positions = portfolio.list_positions()


def _fmt_pnl(v: float | None) -> str:
    if v is None:
        return "—"
    return f"{v:+,.2f}"


with tab_list:
    if not positions:
        st.info("暂无持仓。去「添加持仓」录入第一只。")
    else:
        df = pd.DataFrame([{
            "代码": p["ticker"], "名称": p.get("name", ""),
            "成本价": p["cost_price"], "股数": p["shares"],
            "买入日期": p.get("buy_date", ""), "备注": p.get("note", ""),
        } for p in positions])
        edited = st.data_editor(
            df, num_rows="dynamic", key="pos_editor",
            column_config={
                "代码": st.column_config.TextColumn(required=True),
                "成本价": st.column_config.NumberColumn(min_value=0.0, format="%.3f"),
                "股数": st.column_config.NumberColumn(min_value=0, format="%d"),
            })
        if st.button("保存修改", key="pos_save"):
            try:
                new_tickers = [str(r["代码"]).strip() for r in edited.to_dict("records")]
                if any(not t for t in new_tickers):
                    st.error("代码不能为空")
                elif len(set(new_tickers)) != len(new_tickers):
                    st.error("存在重复代码")
                else:
                    existing = {p["ticker"]: p for p in positions}
                    to_del = [t for t in existing if t not in new_tickers]
                    for t in to_del:
                        portfolio.remove_position(t)
                    for r in edited.to_dict("records"):
                        t = str(r["代码"]).strip()
                        fields = {
                            "cost_price": float(r["成本价"] or 0),
                            "shares": int(r["股数"] or 0),
                            "buy_date": str(r["买入日期"] or ""),
                            "note": str(r["备注"] or ""),
                        }
                        if t in existing:
                            portfolio.update_position(t, **fields)
                        else:
                            portfolio.add_position(t, fields["cost_price"],
                                                   fields["shares"],
                                                   buy_date=fields["buy_date"] or None,
                                                   note=fields["note"])
                    st.success("持仓已更新")
                    st.rerun()
            except Exception as e:
                st.error(f"保存失败: {e}")


with tab_add:
    c2, c3, c4 = st.columns(3)
    ticker = ticker_input("股票代码", key="pf_ticker")
    cost_price = c2.number_input("成本价（元）", min_value=0.0, value=0.0, step=0.01)
    shares = c3.number_input("股数", min_value=1, value=100, step=100)
    buy_date = c4.date_input("买入日期", value=date.today())
    note = st.text_input("备注", placeholder="可选，如：定增解禁临近")
    if st.button("添加", type="primary", key="pos_add"):
        if not ticker.strip():
            st.error("请输入股票代码")
        elif cost_price <= 0:
            st.error("成本价必须大于 0")
        else:
            try:
                portfolio.add_position(ticker.strip(), cost_price, int(shares),
                                       buy_date=buy_date.isoformat(),
                                       note=note.strip())
                st.success(f"已添加 {ticker.strip()}")
                st.rerun()
            except ValueError as e:
                st.error(str(e))


with tab_follow:
    snap = portfolio.load_snapshot(date.today().isoformat())
    if snap:
        m1, m2, m3 = st.columns(3)
        m1.metric("持仓市值（元）", f"{snap.get('total_value', 0):,.2f}")
        m2.metric("浮动盈亏（元）", _fmt_pnl(snap.get("total_pnl")),
                  delta=f"{snap.get('pnl_pct', 0):+.2f}%")
        m3.metric("股票数", len(snap.get("positions", [])))
        st.caption(f"快照 {snap.get('date')} · 已跟进 {len(snap.get('results', []))} 只")
    else:
        st.info("今日尚未生成快照。点击下方按钮盘后跟进（每日 15:15 launchd 自动执行，手动按钮用于补跑/重跑）。")

    c1, c2 = st.columns([1, 3])
    follow_date = c1.date_input("跟进日期", value=date.today())
    force = c1.checkbox("强制重跑（当日已跟进）", value=False)
    if c1.button("开始跟进", type="primary", key="pf_run"):
        if not positions:
            st.error("先添加持仓")
        else:
            def _follow():
                return portfolio.run_daily_follow(
                    follow_date.isoformat(), provider=provider,
                    model=model or "deepseek-v4-flash", force=force)

            job_id = start_job("portfolio", _follow)
            st.session_state["pf_job"] = job_id
            st.session_state.pop("pf_result", None)

    job = get_job(st.session_state.get("pf_job", "")) or get_job(latest_job_id("portfolio") or "") or {}
    status = job.get("status")
    if status in ("queued", "running"):
        @st.fragment(run_every=2)
        def _poll_pf():
            j = get_job(st.session_state.get("pf_job", "")) or get_job(latest_job_id("portfolio") or "") or {}
            if j.get("status") in ("queued", "running"):
                st.info("跟进中（逐票 Ch0 + LLM 决策）… 可切换页面，任务不中断")
            else:
                st.rerun()

        _poll_pf()
    elif status == "error":
        st.error(f"跟进失败: {job['error']}")
        if st.button("清除", key="pf_clear"):
            clear_job(st.session_state.pop("pf_job", latest_job_id("portfolio") or ""))
    elif status == "done":
        result = job.get("result") or {}
        if result.get("skipped"):
            st.warning("当日已跟进，跳过（勾选“强制重跑”可覆盖）")
        elif result.get("results"):
            st.success(f"跟进完成：成功 {len(result['results']) - result.get('failed', 0)} / "
                       f"共 {len(result['results'])} · 失败 {result.get('failed', 0)}")
            rows = []
            for r in result["results"]:
                d = r.get("direction") or "未解析"
                rows.append({
                    "代码": r["ticker"], "名称": r.get("name", ""),
                    "方向": d, "置信度": r.get("confidence") or "—",
                    "成本(¥)": f"{r.get('cost_cny') or 0:.4f}",
                    "深度复核": (f"{r.get('deep_review_signal') or '待复核'}"
                                 if r.get("deep_review") else "—"),
                    "复核报告": r.get("deep_review_path") or "",
                    "报告": r.get("report_path") or "",
                    "错误": r.get("error") or "",
                })
            st.dataframe(pd.DataFrame(rows), hide_index=True)
            if result.get("deep_reviewed"):
                st.info(f"{result['deep_reviewed']} 只持仓触发主线深度复核"
                        "（方向=卖出 或 高危异动：散户接力/融资风险/过热）")
            snap2 = result.get("snapshot") or {}
            if snap2.get("total_value"):
                a, b, c = st.columns(3)
                a.metric("持仓市值（元）", f"{snap2.get('total_value', 0):,.2f}")
                b.metric("浮动盈亏（元）", _fmt_pnl(snap2.get("total_pnl")),
                         delta=f"{snap2.get('pnl_pct', 0):+.2f}%")
                c.metric("失败数", result.get("failed", 0))
            if st.button("清除结果", key="pf_done_clear"):
                clear_job(st.session_state.pop("pf_job", ""))
                st.rerun()


with tab_snap:
    dates = portfolio.list_snapshots()
    if not dates:
        st.info("暂无历史快照。")
    else:
        sel = st.selectbox("选择日期", dates, key="pf_snap_date")
        s = portfolio.load_snapshot(sel) or {}
        if s.get("positions"):
            rows = []
            for r in s["positions"]:
                pnl = r.get("pnl")
                rows.append({
                    "代码": r["ticker"], "名称": r.get("name", ""),
                    "现价": r.get("last_close") or "—",
                    "成本价": r["cost_price"], "股数": r["shares"],
                    "市值(¥)": f"{r.get('market_value', 0):,.2f}",
                    "盈亏(¥)": _fmt_pnl(pnl),
                    "盈亏%": f"{r.get('pnl_pct', 0):+.2f}%" if pnl is not None else "—",
                    "错误": r.get("error") or "",
                })
            st.dataframe(pd.DataFrame(rows), hide_index=True)
        if s.get("results"):
            st.subheader("跟进结果")
            st.dataframe(pd.DataFrame([{
                "代码": r["ticker"], "名称": r.get("name", ""),
                "方向": r.get("direction") or "未解析",
                "置信度": r.get("confidence") or "—",
                "成本(¥)": f"{r.get('cost_cny') or 0:.4f}",
                "深度复核": (f"{r.get('deep_review_signal') or '待复核'}"
                             if r.get("deep_review") else "—"),
                "错误": r.get("error") or "",
            } for r in s["results"]]), hide_index=True)
