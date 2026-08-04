"""误差分析报告 + 短线自校准注入扩展（plan 设计 Step 5）。

数据源：注册表索引 validation 字段（validate.py 已同步），零重算零读盘。
- scored_records():  validation.verdict ∈ {对, 错} 的记录
- build_feedback_report(): 按方向/置信度/模式/异动类型聚合错判率，写
  <registry>/reports/feedback_<asof>.md
- feedback_injection_block(ticker): 该票近期验证结论 + 方向级错判警示，
  追加进短线 prompt（pipeline.py 已接线，无数据返回空串，prompt 零变化）。

安全边界：全部读索引零异常外抛；注入失败返回空串不影响主流程。
"""

from __future__ import annotations

import tempfile
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Optional

from .registry import query, registry_dir

_SCORED = ("对", "错")


def _fmt(v) -> str:
    return f"{v:+.2f}%" if isinstance(v, (int, float)) else "-"


def scored_records() -> list[dict]:
    """注册表内已评分（对/错）记录，含 validation 字段。"""
    out = []
    for r in query():
        v = r.get("validation") or {}
        if v.get("verdict") in _SCORED:
            out.append({**r, "validation": v})
    return out


def _group_stats(recs: list[dict], key_fn) -> dict[str, dict]:
    """按维度分组：{label: {count, wins, loss_rate, avg_t3}}。"""
    groups: dict[str, dict] = {}
    t3s: dict[str, list] = defaultdict(list)
    for r in recs:
        label = key_fn(r) or "未知"
        g = groups.setdefault(label, {"count": 0, "wins": 0, "loss_rate": None,
                                      "avg_t3": None})
        g["count"] += 1
        if r["validation"]["verdict"] == "对":
            g["wins"] += 1
        t = r["validation"].get("t3_close_pct")
        if isinstance(t, (int, float)):
            t3s[label].append(t)
    for label, g in groups.items():
        g["loss_rate"] = round((g["count"] - g["wins"]) / g["count"] * 100, 1)
        vals = t3s[label]
        if vals:
            g["avg_t3"] = round(sum(vals) / len(vals), 2)
    return groups


def _render_table(title: str, groups: dict[str, dict]) -> list[str]:
    lines = [f"### {title}", "",
             "| 分组 | 样本 | 胜 | 负 | 错判率 | 平均T+3 |",
             "|---|---|---|---|---|---|"]
    for label in sorted(groups, key=lambda k: -groups[k]["count"]):
        g = groups[label]
        lines.append(
            f"| {label} | {g['count']} | {g['wins']} | {g['count'] - g['wins']} "
            f"| {g['loss_rate']}% | {_fmt(g['avg_t3'])} |")
    lines.append("")
    return lines


def build_feedback_report(asof: Optional[str] = None) -> dict[str, Any]:
    """生成误差分析报告并落盘 <registry>/reports/feedback_<asof>.md。

    返回 {asof, path, total, wins, losses, win_rate, markdown}。
    无评分记录也正常生成（空报告），不抛。
    """
    asof = asof or time.strftime("%Y-%m-%d")
    recs = scored_records()
    total = len(recs)
    wins = sum(1 for r in recs if r["validation"]["verdict"] == "对")
    losses = total - wins
    win_rate = round(wins / total * 100, 1) if total else None

    by_direction = _group_stats(recs, lambda r: (r.get("summary") or {}).get("direction"))
    by_confidence = _group_stats(recs, lambda r: (r.get("summary") or {}).get("confidence"))
    by_mode = _group_stats(recs, lambda r: (r.get("summary") or {}).get("mode"))
    by_anomaly = _group_stats(
        recs, lambda r: ((r.get("summary") or {}).get("anomaly_types") or [None])[0])

    worst = sorted(
        ((l, g) for l, g in by_direction.items() if g["count"] >= 2),
        key=lambda kv: kv[1]["loss_rate"], reverse=True)

    md = [f"# 验证误差分析报告（{asof}）", "",
          f"- 已评分: {total} ｜ 对: {wins} ｜ 错: {losses}",
          f"- 总体胜率: {win_rate}%" if win_rate is not None else "- 总体胜率: 无样本",
          ""]
    if worst:
        label, g = worst[0]
        md.append(f"- 最大误差源: 方向「{label}」错判率 {g['loss_rate']}%"
                  f"（{g['count']} 样本）")
        md.append("")
    md += _render_table("按方向", by_direction)
    md += _render_table("按置信度", by_confidence)
    md += _render_table("按模式", by_mode)
    md += _render_table("按触发异动类型", by_anomaly)

    md += ["### 错判明细（最近 10 条）", "",
           "| 日期 | 方式 | 代码 | 方向 | 判定 | T+3 | 依据 |",
           "|---|---|---|---|---|---|---|"]
    for r in sorted(recs, key=lambda x: -(x.get("ts") or 0))[:10]:
        v = r["validation"]
        s = r.get("summary") or {}
        md.append(
            f"| {r.get('trade_date')} | {r.get('kind')} | {r.get('ticker')} "
            f"| {s.get('direction') or '-'} | {v.get('verdict')} "
            f"| {_fmt(v.get('t3_close_pct'))} | {str(v.get('verdict_basis') or '')[:48]} |")
    md.append("")

    text = "\n".join(md)
    path = registry_dir() / "reports" / f"feedback_{asof}.md"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        tmp.replace(path)
    except OSError:
        pass
    return {"asof": asof, "path": str(path), "total": total, "wins": wins,
            "losses": losses, "win_rate": win_rate, "markdown": text}


def feedback_injection_block(ticker: str, n: int = 5) -> str:
    """该票近期验证结论 + 方向级错判警示，返回注入块（无数据 → 空串）。

    方向警示：该票最近一条评分结论的方向，若全市场同方向错判率 ≥ 50%，
    强制提示"先论证为何这次不同"再给判断。
    """
    recs = [r for r in query(ticker=ticker)
            if (r.get("validation") or {}).get("verdict") in _SCORED]
    recs.sort(key=lambda r: -(r.get("ts") or 0))
    if not recs:
        return ""

    lines = ["## 该标的近期验证结论（复盘参考）"]
    for r in recs[:n]:
        v = r["validation"]
        s = r.get("summary") or {}
        d = s.get("direction") or "?"
        lines.append(
            f"- {r.get('trade_date')} 方向={d}：判定「{v.get('verdict')}」"
            f" T+3 {_fmt(v.get('t3_close_pct'))} "
            f"T+10 {_fmt(v.get('t10_close_pct'))}（{str(v.get('verdict_basis') or '')[:60]}）")

    last_dir = (recs[0].get("summary") or {}).get("direction")
    if last_dir:
        same = [r for r in scored_records()
                if (r.get("summary") or {}).get("direction") == last_dir]
        if same:
            wins = sum(1 for r in same if r["validation"]["verdict"] == "对")
            loss_rate = round((len(same) - wins) / len(same) * 100, 1)
            if loss_rate >= 50:
                lines.append(
                    f"- ⚠ 全市场同方向（{last_dir}）近 {len(same)} 条错判率 "
                    f"{loss_rate}%：本次若仍给「{last_dir}」，必须先论证"
                    f"「为何这次与多数错判不同」，再给判断，禁止默认延续。")
    lines.append("纪律：以上仅为统计参考，不代表本票走势；判断仍以当日数据为准。")
    return "\n".join(lines)


__all__ = ["build_feedback_report", "feedback_injection_block", "scored_records"]
