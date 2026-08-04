"""分析结果对比引擎（规则引擎为主，LLM 深度对比可选）。

- build_rule_compare: 两条记录字段级 diff（方向翻转/评级升降/目标止损移动/置信度/收盘价/异动），
  纯函数零 LLM，自动流程唯一入口。
- after_register: register() 后自动触发（同票最近两条规则对比并落盘），任何异常静默。
- build_timeline_compare: 单票多条记录对齐表（UI 全量对比数据源）。
- build_llm_compare: 读取两条记录原文做深度对比，仅手动调用，成本记账 feature="compare"。

落盘: <registry>/compare/<ticker>_<older_id>__vs__<newer_id>.json（id 冒号转下划线）。
"""

from __future__ import annotations

import json
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from ..cost import tracker as _cost
from .registry import registry_dir

# 参与规则对比的 summary 字段（label=UI 列名，ftype=px 数字参与百分比 diff）
_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("direction", "方向", "text"),
    ("rating", "评级", "text"),
    ("confidence", "置信度", "text"),
    ("horizon", "适用周期", "text"),
    ("target_raw", "目标价", "text"),
    ("stop_raw", "止损位", "text"),
    ("last_close", "收盘价", "px"),
    ("mode", "模式", "text"),
)

_LLM_READ_LIMIT = 6000  # 每份报告截断字符数


def _atomic_write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent,
        prefix=path.stem + ".", suffix=".tmp", delete=False,
    ) as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
        tmp = Path(f.name)
    tmp.replace(path)


def _meta(rec: dict) -> dict:
    return {k: rec.get(k) for k in
            ("id", "kind", "ticker", "name", "trade_date", "ts", "path", "summary")}


def build_rule_compare(older: dict, newer: dict) -> Optional[dict]:
    """两条记录字段级规则对比。同 id → None。"""
    if not older or not newer or older.get("id") == newer.get("id"):
        return None
    so, sn = older.get("summary") or {}, newer.get("summary") or {}
    key_metrics: list[dict] = []
    changes: list[str] = []
    same_fields: list[str] = []
    for field, label, ftype in _FIELDS:
        ov, nv = so.get(field), sn.get(field)
        if ov is None and nv is None:
            continue
        row: dict[str, Any] = {"field": label, "old": ov, "new": nv}
        if ov == nv:
            row["change"] = "same"
            same_fields.append(label)
        elif ov is None:
            row["change"] = "new"
            changes.append(f"{label}：新增 {nv}")
        elif nv is None:
            row["change"] = "gone"
            changes.append(f"{label}：{ov} → 消失")
        else:
            row["change"] = "flip" if field == "direction" else "changed"
            if (ftype == "px" and isinstance(ov, (int, float))
                    and isinstance(nv, (int, float)) and ov):
                delta = (nv / ov - 1) * 100
                changes.append(f"{label}：{ov} → {nv}（{delta:+.2f}%）")
            else:
                changes.append(f"{label}：{ov} → {nv}")
        key_metrics.append(row)

    ao, an = set(so.get("anomaly_types") or []), set(sn.get("anomaly_types") or [])
    if ao != an:
        key_metrics.append({"field": "异动", "old": sorted(ao), "new": sorted(an),
                            "change": "changed"})
        added, removed = sorted(an - ao), sorted(ao - an)
        if added:
            changes.append(f"新增异动：{'、'.join(added)}")
        if removed:
            changes.append(f"异动消失：{'、'.join(removed)}")

    return {
        "ticker": newer.get("ticker") or older.get("ticker"),
        "older": _meta(older), "newer": _meta(newer),
        "key_metrics": key_metrics, "changes": changes, "same_fields": same_fields,
        "rule": True, "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def build_timeline_compare(records: list[dict]) -> dict:
    """单票多条记录全量对齐表（旧→新）+ 相邻对比摘要。UI 全量对比数据源。"""
    rows = sorted(records, key=lambda r: (r.get("trade_date") or "",
                                          int(r.get("ts", 0) or 0)))
    columns = {}
    for field, label, _ftype in _FIELDS:
        columns[field] = {"label": label,
                          "values": [(r.get("summary") or {}).get(field)
                                     for r in rows]}
    adjacent = []
    for i in range(len(rows) - 1):
        c = build_rule_compare(rows[i], rows[i + 1])
        if c:
            adjacent.append(c)
    return {
        "ticker": records[0].get("ticker", "") if records else "",
        "records": [_meta(r) for r in rows],
        "columns": columns, "adjacent": adjacent,
    }


# ---------------------------------------------------------------------------
# 落盘 / 读取
# ---------------------------------------------------------------------------

def _compare_dir() -> Path:
    return registry_dir() / "compare"


def save_compare_report(payload: dict, ticker: str) -> Optional[Path]:
    """落盘对比报告。同对（id 相同）幂等不重写。失败返回 None。"""
    try:
        name = (f"{ticker}_{payload['older']['id']}__vs__"
                f"{payload['newer']['id']}").replace(":", "_")
        path = _compare_dir() / f"{name}.json"
        if path.exists():
            return path
        _atomic_write(path, payload)
        return path
    except Exception:
        return None


def load_compare_reports(ticker: Optional[str] = None) -> list[dict]:
    """读取对比报告（新→旧）。ticker 过滤（文件前缀匹配）。"""
    d = _compare_dir()
    if not d.exists():
        return []
    out = []
    for p in d.glob("*.json"):
        if ticker and not p.name.startswith(f"{ticker}_"):
            continue
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    out.sort(key=lambda r: r.get("created_at", ""), reverse=True)
    return out


def latest_compare_for(ticker: str) -> Optional[dict]:
    """该票最新一份对比报告。"""
    rs = load_compare_reports(ticker)
    return rs[0] if rs else None


# ---------------------------------------------------------------------------
# 自动触发（register 后调用；同票最近两条规则对比）
# ---------------------------------------------------------------------------

def after_register(kind: str, ticker: str) -> None:
    """注册完成后自动对比：同票最近两条记录 → 规则对比落盘。异常静默。"""
    if not ticker or kind in ("screen", "pick"):
        return
    try:
        from .registry import query

        rs = query(ticker=ticker, limit=2)
        if len(rs) < 2:
            return
        newer, older = rs[0], rs[1]
        if newer["id"] == older["id"]:
            return
        payload = build_rule_compare(older, newer)
        if payload:
            save_compare_report(payload, ticker)
    except Exception:
        return


# ---------------------------------------------------------------------------
# LLM 深度对比（仅手动；读原文 + 成本记账）
# ---------------------------------------------------------------------------

def _read_truncated(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read(_LLM_READ_LIMIT + 1)
        return text[:_LLM_READ_LIMIT]
    except OSError:
        return ""


def build_llm_compare(rec_a: dict, rec_b: dict, provider: str = "deepseek",
                      model: str = "deepseek-v4-flash",
                      base_url: Optional[str] = None) -> dict:
    """读取两条记录原文做 LLM 深度对比。失败返回 {"ok": False, "error"}。"""
    run_id = uuid.uuid4().hex
    tokens = _cost.cost_context("compare", run_id)
    try:
        from ..shortterm.pipeline import build_llm

        text_a = _read_truncated(rec_a.get("path", ""))
        text_b = _read_truncated(rec_b.get("path", ""))
        if not text_a and not text_b:
            return {"ok": False, "error": "两条记录均无可读原文"}
        meta_a = f"{rec_a.get('kind')} {rec_a.get('ticker')} {rec_a.get('trade_date')}"
        meta_b = f"{rec_b.get('kind')} {rec_b.get('ticker')} {rec_b.get('trade_date')}"
        prompt = (
            "你是一名A股投研分析师。以下是同一标的两次分析的报告片段（已截断）。\n\n"
            f"【分析A】{meta_a}\n{text_a}\n\n"
            f"【分析B】{meta_b}\n{text_b}\n\n"
            "请输出对比分析（中文，400字以内）：\n"
            "1. 核心判断变化：方向/评级/目标位如何变，一句话概括\n"
            "2. 理由支撑：B 相对 A 判断变化的关键论据\n"
            "3. 新增风险/机会点\n"
            "4. 操作含义：一句话\n"
        )
        llm = build_llm(provider, model, base_url)
        resp = llm.invoke(prompt)
        text = resp.content if hasattr(resp, "content") else str(resp)
        return {"ok": True, "text": str(text), "meta_a": meta_a, "meta_b": meta_b,
                "cost": _cost.run_summary(run_id),
                "created_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    finally:
        _cost.reset_cost_context(tokens)
