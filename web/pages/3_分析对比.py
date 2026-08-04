"""分析对比 — 统一注册表：同票纵向对比（规则引擎 + LLM 深度对比）+ 验证结论 + 误差报告。

数据源: ~/.tradingagents/analysis_registry/
  index.json   — 全方式关联索引（短线/持仓/扫描/深复核/主线）
  compare/     — 自动规则对比报告
  validate/    — 盘后自动验证结论
  reports/     — 误差分析报告
"""

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

load_dotenv(_PROJECT_ROOT / ".env", override=True)

from tradingagents.analysis_registry import registry  # noqa: E402
from tradingagents.analysis_registry.compare import (  # noqa: E402
    build_llm_compare,
    load_compare_reports,
    save_compare_report,
)
from tradingagents.analysis_registry.validate import (  # noqa: E402
    load_validation,
    validate_record,
)
from tradingagents.analysis_registry.feedback import build_feedback_report  # noqa: E402
from web.ui_theme import apply_theme  # noqa: E402

st.set_page_config(page_title="分析对比", page_icon="🔀", layout="wide")
apply_theme()

st.title("🔀 分析对比")
st.caption("注册表关联：同票纵向对比（规则/LLM）· 盘后验证结论 · 误差分析（launchd 自动 15:35 验证）")


def _tickers() -> list[str]:
    seen: dict[str, str] = {}
    for r in registry.query():
        t = r.get("ticker") or ""
        if t and t not in seen:
            seen[t] = r.get("name") or ""
    return [f"{t} {seen[t]}" for t in sorted(seen)]


def _pick_ticker() -> str | None:
    options = _tickers()
    if not options:
        st.info("注册表暂无记录：先跑一次短线分析/扫描/深度复核，或等待盘后验证落盘。")
        return None
    sel = st.selectbox("选择标的（注册表内全方式记录）", options)
    return sel.split(" ")[0]


# ---------------------------------------------------------------------------
# 同票时间线 + 纵向对比
# ---------------------------------------------------------------------------
st.subheader("① 同票时间线（跨方式纵向关联）")

ticker = _pick_ticker()
if ticker:
    recs = registry.query(ticker=ticker)
    recs.sort(key=lambda r: (r.get("trade_date") or "", r.get("ts") or 0))
    if not recs:
        st.warning(f"{ticker} 无注册表记录")
        ticker = None

if ticker:
    rows = []
    for r in recs:
        s = r.get("summary") or {}
        v = r.get("validation") or {}
        kind = r.get("kind")
        label = {"stock": "短线", "follow": "持仓跟进", "screen": "扫描",
                 "pick": "扫描推荐", "deep_review": "深复核", "mainline": "主线"}.get(kind, kind)
        d = s.get("direction") or s.get("rating") or "-"
        rows.append({
            "日期": r.get("trade_date"), "方式": label, "代码": r.get("ticker"),
            "方向/评级": d,
            "置信度": s.get("confidence") or "-",
            "模式": s.get("mode") or "-",
            "收盘": s.get("last_close") or "-",
            "验证": v.get("verdict") or "-",
            "T+3%": v.get("t3_close_pct") if isinstance(v.get("t3_close_pct"), (int, float)) else "-",
            "id": r["id"],
        })
    st.dataframe(
        [ {k: row[k] for k in ("日期", "方式", "代码", "方向/评级", "置信度",
                               "模式", "收盘", "验证", "T+3%")} for row in rows ],
        use_container_width=True, hide_index=True,
    )

    st.subheader("② 规则自动对比（相邻记录字段级 diff）")
    reports = load_compare_reports(ticker)
    if not reports:
        st.info("暂无自动对比报告：同票至少需 2 条注册表记录（每次新记录落盘自动生成）。")
    for rep in reports[:10]:
        with st.expander(
            f"📅 {rep.get('older', {}).get('trade_date')} → "
            f"{rep.get('newer', {}).get('trade_date')} · "
            f"{rep.get('older', {}).get('kind')} → {rep.get('newer', {}).get('kind')}"
        ):
            for ch in rep.get("changes", []):
                st.markdown(f"- **{ch.get('label')}**：{ch.get('old')} → "
                            f"{ch.get('new')}（{ch.get('change')}）")
            if rep.get("same_fields"):
                st.caption("未变：" + "、".join(rep["same_fields"]))
            st.caption(f"报告: {rep.get('path', '')}")

    st.subheader("③ 手动 LLM 深度对比（付费 token，按需）")
    if len(recs) >= 2:
        opts = [f"{r.get('trade_date')} {r.get('kind')} {r.get('id')}" for r in recs]
        older_sel = st.selectbox("旧记录", opts, key="older")
        newer_sel = st.selectbox("新记录", opts, index=1, key="newer")
        if st.button("运行 LLM 深度对比", type="primary"):
            older = recs[opts.index(older_sel)]
            newer = recs[opts.index(newer_sel)]
            with st.spinner("LLM 对比中…"):
                out = build_llm_compare(older, newer)
            if out.get("ok"):
                st.markdown(out.get("text") or "")
                if out.get("cost"):
                    st.caption(f"成本: {out['cost']}")
            else:
                st.error(f"对比失败: {out.get('error')}")
    else:
        st.caption("至少 2 条记录才可深度对比")

# ---------------------------------------------------------------------------
# 验证 + 误差
# ---------------------------------------------------------------------------
st.subheader("④ 事后验证（盘后 15:35 自动，可手动补跑）")
run_col1, run_col2, run_col3 = st.columns([2, 2, 3])
with run_col1:
    if st.button("立即验证：该票全部记录", type="secondary"):
        pending = st.status("验证中…", expanded=False)
        n = 0
        for r in registry.query(ticker=ticker or None):
            out = validate_record(r, force=False)
            n += 1
            pending.write(f"{r.get('kind')} {r.get('ticker')} "
                          f"{r.get('trade_date')}: {out.get('verdict')}")
        pending.update(label=f"完成 {n} 条", state="complete")
        st.rerun()
with run_col2:
    st.caption("单条验证：点上方时间线行内「验证」列（- 表示未评分/待验证）")
with run_col3:
    st.caption("验证结论写入 validate/ + 索引 validation 字段，供上表与误差报告消费")

st.subheader("⑤ 验证误差分析报告（方向/置信度/模式/异动 错判率）")
if st.button("生成/刷新误差报告"):
    with st.spinner("聚合中…"):
        rep = build_feedback_report()
    st.success(f"已生成 {rep['path']}（评分 {rep['total']} 条，胜率 {rep['win_rate']}%）")
    st.markdown(rep["markdown"])
