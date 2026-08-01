"""短线决策落盘 + 事后评估（复盘闭环 v1/v2 共用）。

存储: ~/.tradingagents/shortterm/
  个股: <ticker>_<trade_date>_<ts>.json
  选股: screener_<trade_date>_<ts>.json
"""

from __future__ import annotations

import json
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

_DIR = Path.home() / ".tradingagents" / "shortterm"
_LOCK = threading.Lock()

_DIRECTION_RE = re.compile(r"\*\*方向\*\*[:：]\s*(买入|观望|卖出|回避)")
_CONFIDENCE_RE = re.compile(r"\*\*置信度\*\*[:：]\s*(高|中|低)")


def parse_decision(report: str) -> dict[str, str | None]:
    """从决策卡 markdown 提取方向/置信度（DECISION_CARD_FORMAT 强制格式）。"""
    d = _DIRECTION_RE.search(report or "")
    c = _CONFIDENCE_RE.search(report or "")
    return {"direction": d.group(1) if d else None,
            "confidence": c.group(1) if c else None}


def _atomic_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent,
        prefix=path.stem + ".", suffix=".tmp", delete=False,
    ) as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
        tmp = Path(f.name)
    tmp.replace(path)


def save_stock_record(result: dict, inputs: dict) -> Path | None:
    """落盘个股决策。ch0_only（无 LLM 报告）跳过。返回路径。"""
    if result.get("mode") == "ch0_only":
        return None
    ch0 = result["ch0"]
    report = result.get("report", "")
    ts = int(time.time())
    payload = {
        "kind": "stock",
        "ticker": ch0["ticker"],
        "name": ch0.get("name", ""),
        "trade_date": ch0["trade_date"],
        "ts": ts,
        "mode": result.get("mode"),
        "inputs": inputs,
        "ch0": ch0,
        "bundle": result.get("bundle"),
        "report": report,
        "parsed": parse_decision(report),
    }
    path = _DIR / f"{ch0['ticker']}_{ch0['trade_date']}_{ts}.json"
    with _LOCK:
        _atomic_write(path, payload)
    return path


def save_screen_record(scan_result: dict, report: str | None) -> Path:
    ts = int(time.time())
    payload = {
        "kind": "screen",
        "trade_date": scan_result["trade_date"],
        "ts": ts,
        "capital": scan_result.get("capital"),
        "scan": scan_result,
        "report": report,
    }
    path = _DIR / f"screener_{scan_result['trade_date']}_{ts}.json"
    with _LOCK:
        _atomic_write(path, payload)
    return path


def list_records(ticker: str | None = None, kind: str | None = None) -> list[dict]:
    """扫描落盘记录，返回摘要列表（新→旧）。摘要不含 ch0/bundle/report 全文。"""
    if not _DIR.exists():
        return []
    out = []
    for p in _DIR.glob("*.json"):
        try:
            with open(p, encoding="utf-8") as f:
                r = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        if ticker and r.get("ticker") != ticker:
            continue
        if kind and r.get("kind") != kind:
            continue
        out.append({
            "path": str(p),
            "kind": r.get("kind"),
            "ticker": r.get("ticker"),
            "name": r.get("name", ""),
            "trade_date": r.get("trade_date"),
            "ts": r.get("ts", 0),
            "mode": r.get("mode"),
            "parsed": r.get("parsed") or {},
        })
    out.sort(key=lambda e: -e["ts"])
    return out


def load_record(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# 事后评估：建议方向 vs 实际走势
# ---------------------------------------------------------------------------

def evaluate_call(record: dict, asof_date: str | None = None) -> dict[str, Any]:
    """以记录日收盘为基准，计算 T+1开/收、T+3、T+10 收益并判定对错。

    判定规则（plan 定稿）:
    - 买入: T+3 收益 > 0 → 对，否则错
    - 卖出: T+3 收益 < 0 → 对，否则错
    - 观望/回避/未解析: 不评分（不可证伪），只展示收益
    - 后续 K线不足 3 根: 待验证
    """
    from tradingagents.dataflows.a_stock import _load_ohlcv_astock

    ticker = record["ticker"]
    rec_date = record["trade_date"]
    entry_close = (record.get("ch0", {}).get("metrics") or {}).get("last_close")
    asof = asof_date or time.strftime("%Y-%m-%d")

    out: dict[str, Any] = {
        "entry_close": entry_close, "asof": asof,
        "t1_open_pct": None, "t1_close_pct": None,
        "t3_close_pct": None, "t10_close_pct": None,
        "bars_after": 0, "verdict": "不评分", "verdict_basis": "",
    }
    if not entry_close:
        out["verdict_basis"] = "记录缺基准收盘价"
        return out

    df = _load_ohlcv_astock(ticker, asof)
    after = df[df["Date"] > rec_date].sort_values("Date")
    out["bars_after"] = len(after)
    if len(after) == 0:
        out["verdict"] = "待验证"
        out["verdict_basis"] = "记录日之后暂无K线"
        return out

    def _pct(px):
        return round((px / entry_close - 1) * 100, 2)

    t1 = after.iloc[0]
    out["t1_open_pct"] = _pct(float(t1["Open"]))
    out["t1_close_pct"] = _pct(float(t1["Close"]))
    if len(after) >= 3:
        out["t3_close_pct"] = _pct(float(after.iloc[2]["Close"]))
    if len(after) >= 10:
        out["t10_close_pct"] = _pct(float(after.iloc[9]["Close"]))

    direction = (record.get("parsed") or {}).get("direction")
    if direction not in ("买入", "卖出"):
        out["verdict_basis"] = f"方向={direction or '未解析'}，不评分"
        return out
    if out["t3_close_pct"] is None:
        out["verdict"] = "待验证"
        out["verdict_basis"] = f"后续仅 {len(after)} 根K线，不足 T+3"
        return out

    r3 = out["t3_close_pct"]
    hit = (r3 > 0) if direction == "买入" else (r3 < 0)
    out["verdict"] = "对" if hit else "错"
    out["verdict_basis"] = f"{direction} vs T+3 收益 {r3:+.2f}%"
    return out
