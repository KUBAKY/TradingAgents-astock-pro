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


def _save_env_key(key_name: str, value: str):
    env_path = _PROJECT_ROOT / ".env"
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    prefix = f"{key_name}="
    for i, line in enumerate(lines):
        if line.startswith(prefix):
            lines[i] = f"{prefix}{value}"
            break
    else:
        lines.append(f"{prefix}{value}")
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


with st.sidebar:
    st.header("LLM 设置")
    provider = st.selectbox("Provider", ["anthropic", "deepseek", "minimax", "qwen", "glm", "openai_compatible"], index=0)
    _DEFAULT_MODELS = {
        "anthropic": "claude-haiku-4-5", "deepseek": "deepseek-chat",
        "minimax": "MiniMax-M2.7", "qwen": "qwen-plus", "glm": "glm-4",
        "openai_compatible": "",
    }
    model = st.text_input("Model", value=_DEFAULT_MODELS.get(provider, ""))
    base_url = st.text_input("Base URL（可选）", value="") or None

    key_name = _ENV_KEYS[provider]
    api_key = st.text_input(
        f"API Key（{key_name}）", type="password",
        value=os.environ.get(key_name, ""),
        help="仅保存在本机。勾选下方保存则写入项目根目录 .env，重启后免输",
    )
    if api_key:
        os.environ[key_name] = api_key
    if api_key and st.checkbox("保存 API Key 到 .env"):
        _save_env_key(key_name, api_key)
        st.success(f"已写入 .env 的 {key_name}")

    has_key = bool(os.environ.get(key_name))
    if not has_key:
        st.warning(f"未检测到 {key_name}，分析会报 401")
    st.caption("anthropic 走 requests 直连（绕开 headroom-proxy 的 httpx 502）")

tab_stock, tab_screen = st.tabs(["个股决策", "全市场选股"])


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
    st.download_button("下载报告 (md)", result["report"],
                       file_name=f"{ch0['ticker']}_{ch0['trade_date']}_shortterm.md")


with tab_stock:
    c1, c2, c3 = st.columns(3)
    ticker = c1.text_input("股票代码", value="", placeholder="如 000725")
    trade_date = c2.date_input("分析日期", value=date.today()).strftime("%Y-%m-%d")
    intent = c3.text_input("你的诉求（可选）", placeholder="如：连板想追 / 被套要不要割")

    c4, c5, c6 = st.columns(3)
    capital = c4.number_input("总资金（元）", min_value=0, value=0, step=10000) or None
    cost = c5.number_input("持仓成本价（被套分析用）", min_value=0.0, value=0.0, step=0.01) or None
    shares = c6.number_input("持仓股数", min_value=0, value=0, step=100) or None

    ch0_only = st.checkbox("只跑 Ch0 扫描（不调 LLM，免费）", value=False)

    if st.button("开始分析", type="primary", key="stock_run"):
        if not ticker.strip():
            st.error("请输入股票代码")
        else:
            with st.spinner("分析中（Ch0扫描 → 数据包 → LLM决策）..."):
                try:
                    result = pipeline.run(
                        ticker.strip(), trade_date, intent, capital, cost, shares,
                        provider, model or "claude-haiku-4-5", base_url, ch0_only,
                    )
                    st.session_state["st_result"] = result
                except Exception as e:
                    st.error(f"分析失败: {e}")
    if st.session_state.get("st_result"):
        show_result(st.session_state["st_result"])

with tab_screen:
    s1, s2 = st.columns(2)
    s_capital = s1.number_input("可用资金（元）", min_value=0, value=200000, step=10000, key="s_cap") or None
    per_board = s2.slider("每板块精扫候选数", 3, 15, 8)
    s_no_llm = st.checkbox("只输出扫描JSON（不调LLM）", value=False, key="s_nollm")

    if st.button("开始扫描", type="primary", key="screen_run"):
        with st.spinner("全市场扫描中（约1-2分钟：快照→粗排→Ch0精扫→LLM推荐）..."):
            try:
                scan_result = screener.scan(s_capital, per_board)
                if s_no_llm:
                    st.session_state["sc_result"] = ("json", scan_result)
                else:
                    text = screener.recommend(scan_result, provider, model or "claude-haiku-4-5", base_url)
                    st.session_state["sc_result"] = ("report", text, scan_result)
            except Exception as e:
                st.error(f"扫描失败: {e}")

    sc = st.session_state.get("sc_result")
    if sc:
        if sc[0] == "json":
            st.json(sc[1])
        else:
            st.markdown(sc[1])
            with st.expander("候选池原始数据"):
                st.json(sc[2])
            st.download_button("下载推荐 (md)", sc[1], file_name="screener.md")
