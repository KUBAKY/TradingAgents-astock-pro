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

_PX_RE = re.compile(r"([-+]?\d+(?:\.\d+)?)")


def _first_price(text: str) -> tuple[str, float] | None:
    """取行内第一个数值；紧邻 % → ('pct', v)，否则 ('px', v)。"""
    m = _PX_RE.search(text or "")
    if not m:
        return None
    num = float(m.group(1))
    tail = text[m.end():m.end() + 2]
    if "%" in tail:
        return ("pct", num)
    return ("px", num)


def parse_price_levels(report: str) -> dict[str, Any]:
    """从决策卡提取目标价/止损位（自由文本容错）。

    返回 {"target": ("px"|"pct", v)|None, "stop": ...,
          "raw_target": str, "raw_stop": str}
    """
    out: dict[str, Any] = {"target": None, "stop": None,
                           "raw_target": "", "raw_stop": ""}
    for line in (report or "").splitlines():
        s = line.strip()
        low = s.replace(" ", "")
        if low.startswith(("-目标价", "目标价")):
            out["raw_target"] = s
            out["target"] = _first_price(s)
        elif low.startswith(("-止损位", "-止损", "止损位")):
            out["raw_stop"] = s
            out["stop"] = _first_price(s)
    return out


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
        "levels": parse_price_levels(report),
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

def evaluate_call(record: dict, asof_date: str | None = None, df=None) -> dict[str, Any]:
    """以记录日收盘为基准，计算 T+1开/收、T+3、T+10 收益并判定对错。

    判定规则（plan 定稿）:
    - 买入: T+3 收益 > 0 → 对，否则错
    - 卖出: T+3 收益 < 0 → 对，否则错
    - 观望/回避/未解析: 不评分（不可证伪），只展示收益
    - 后续 K线不足 3 根: 待验证

    df 可选：预加载的 OHLCV（聚合统计复用同票K线，避免重复读盘）。
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
        # 止损/目标价命中路径
        "raw_stop": "", "raw_target": "", "stop_px": None, "target_px": None,
        "hit_first": None, "hit_bar": None, "hit_pct": None,
    }
    if not entry_close:
        out["verdict_basis"] = "记录缺基准收盘价"
        return out

    if df is None:
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

    # --- 止损/目标价路径扫描（T+10 窗口内，先到者胜；同日双触发记止损先） ---
    levels = record.get("levels") or parse_price_levels(record.get("report", ""))
    stop_raw = levels.get("raw_stop", "") or ""
    target_raw = levels.get("raw_target", "") or ""
    out["raw_stop"], out["raw_target"] = stop_raw, target_raw

    def _resolve(level):
        if not level:
            return None
        kind, v = level
        return entry_close * (1 + v / 100) if kind == "pct" else v

    stop_px = _resolve(levels.get("stop"))
    target_px = _resolve(levels.get("target"))
    if direction == "买入":
        if stop_px is not None and not (stop_px < entry_close):
            stop_px = None
        if target_px is not None and not (target_px > entry_close):
            target_px = None
    else:  # 卖出
        if stop_px is not None and not (stop_px > entry_close):
            stop_px = None
        if target_px is not None and not (target_px < entry_close):
            target_px = None
    out["stop_px"], out["target_px"] = stop_px, target_px

    if stop_px is not None or target_px is not None:
        stop_bar = target_bar = None
        for i in range(min(len(after), 10)):
            bar = after.iloc[i]
            hi, lo = float(bar["High"]), float(bar["Low"])
            if direction == "买入":
                stop_hit = stop_px is not None and lo <= stop_px
                tgt_hit = target_px is not None and hi >= target_px
            else:
                stop_hit = stop_px is not None and hi >= stop_px
                tgt_hit = target_px is not None and lo <= target_px
            if stop_hit and tgt_hit:
                stop_bar = i + 1  # 同日双触发，保守记止损先
                break
            if stop_hit:
                stop_bar = i + 1
                break
            if tgt_hit:
                target_bar = i + 1
                break

        if stop_bar is not None or target_bar is not None:
            if stop_bar is not None and (target_bar is None or stop_bar <= target_bar):
                out["hit_first"] = "止损"
                out["hit_bar"] = stop_bar
                px_hit = stop_px if stop_px is not None else target_px
            else:
                out["hit_first"] = "目标"
                out["hit_bar"] = target_bar
                px_hit = target_px if target_px is not None else stop_px
            if px_hit:
                out["hit_pct"] = round(
                    (px_hit / entry_close - 1) * 100 if direction == "买入"
                    else (entry_close / px_hit - 1) * 100,
                    2,
                )

    if out["t3_close_pct"] is None:
        out["verdict"] = "待验证"
        out["verdict_basis"] = f"后续仅 {len(after)} 根K线，不足 T+3"
        return out

    r3 = out["t3_close_pct"]
    hit = (r3 > 0) if direction == "买入" else (r3 < 0)
    out["verdict"] = "对" if hit else "错"
    out["verdict_basis"] = f"{direction} vs T+3 收益 {r3:+.2f}%"
    return out


def load_past_evaluations(ticker: str, before_date: str, n: int = 3) -> list[dict]:
    """取该票最近 n 条个股决策记录 + 事后评估（评估基准日=before_date，防前视）。

    v2 自校准注入用。任何单条失败静默跳过，不阻塞主流程。
    """
    out = []
    for r in list_records(ticker=ticker, kind="stock"):
        if not r.get("trade_date") or r["trade_date"] >= before_date:
            continue
        try:
            full = load_record(r["path"])
            ev = evaluate_call(full, asof_date=before_date)
        except Exception:
            continue
        out.append({"record": full, "evaluation": ev})
        if len(out) >= n:
            break
    return out


# ---------------------------------------------------------------------------
# 复盘胜率仪表盘聚合
# ---------------------------------------------------------------------------

def aggregate_stats(records: list[dict], asof_date: str | None = None) -> dict[str, Any]:
    """对全部个股决策记录做胜率/平均收益聚合（复盘仪表盘数据源）。

    同 ticker 的 K线只加载一次（df 缓存），评估失败单条跳过。
    返回:
      total/pending/scored, wins/losses, win_rate,
      by_direction: {买入: {count, wins, losses, win_rate, avg_t3}, 卖出: {...}},
      avg_t1_close/avg_t3_close/avg_t10_close（仅评分记录）,
      best/worst: 单条 T+3 最优/最差调用（ticker/date/direction/t3/verdict）,
      recent: 最近 ≤10 条评分结论 [{ticker, name, trade_date, direction, t3, verdict}]
    """
    from tradingagents.dataflows.a_stock import _load_ohlcv_astock

    asof = asof_date or time.strftime("%Y-%m-%d")
    _df_cache: dict[str, Any] = {}
    stats: dict[str, Any] = {
        "asof": asof, "total": 0, "pending": 0, "scored": 0,
        "wins": 0, "losses": 0, "win_rate": None,
        "by_direction": {"买入": {"count": 0, "wins": 0, "losses": 0,
                                 "win_rate": None, "avg_t3": None},
                         "卖出": {"count": 0, "wins": 0, "losses": 0,
                                 "win_rate": None, "avg_t3": None}},
        "avg_t1_close": None, "avg_t3_close": None, "avg_t10_close": None,
        "best": None, "worst": None, "recent": [],
    }

    scored: list[dict] = []
    t1s, t3s, t10s = [], [], []

    for r in records:
        if r.get("kind") != "stock":
            continue
        stats["total"] += 1
        try:
            full = load_record(r["path"])
        except Exception:
            continue
        try:
            ticker = full["ticker"]
            if ticker not in _df_cache:
                _df_cache[ticker] = _load_ohlcv_astock(ticker, asof)
            ev = evaluate_call(full, asof_date=asof, df=_df_cache[ticker])
        except Exception:
            continue

        if ev.get("verdict") in ("待验证", "不评分"):
            stats["pending"] += 1
            continue
        if ev.get("t3_close_pct") is None:
            stats["pending"] += 1
            continue

        direction = (full.get("parsed") or {}).get("direction")
        if direction not in ("买入", "卖出"):
            stats["pending"] += 1
            continue

        stats["scored"] += 1
        verdict = ev["verdict"]
        is_win = verdict == "对"
        if is_win:
            stats["wins"] += 1
        else:
            stats["losses"] += 1

        t3 = ev["t3_close_pct"]
        d = stats["by_direction"][direction]
        d["count"] += 1
        d["wins"] += 1 if is_win else 0
        d["losses"] += 1 if not is_win else 0

        if ev.get("t1_close_pct") is not None:
            t1s.append(ev["t1_close_pct"])
        t3s.append(t3)
        if ev.get("t10_close_pct") is not None:
            t10s.append(ev["t10_close_pct"])

        scored.append({
            "ticker": full.get("ticker"), "name": full.get("name", ""),
            "trade_date": full.get("trade_date"), "direction": direction,
            "t3": t3, "verdict": verdict,
        })

    if stats["scored"]:
        stats["win_rate"] = round(stats["wins"] / stats["scored"] * 100, 1)
    for direction, d in stats["by_direction"].items():
        if d["count"]:
            d["win_rate"] = round(d["wins"] / d["count"] * 100, 1)
            d["avg_t3"] = round(sum(x["t3"] for x in scored
                                   if x["direction"] == direction) / d["count"], 2)
    if t1s:
        stats["avg_t1_close"] = round(sum(t1s) / len(t1s), 2)
    if t3s:
        stats["avg_t3_close"] = round(sum(t3s) / len(t3s), 2)
    if t10s:
        stats["avg_t10_close"] = round(sum(t10s) / len(t10s), 2)

    if scored:
        stats["best"] = max(scored, key=lambda x: x["t3"])
        stats["worst"] = min(scored, key=lambda x: x["t3"])
    scored.sort(key=lambda x: x["trade_date"], reverse=True)
    stats["recent"] = scored[:10]
    return stats
