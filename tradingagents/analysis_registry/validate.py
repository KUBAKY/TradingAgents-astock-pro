"""统一验证引擎（跨方式事后评估，plan 设计 Step 4）。

对注册表内每条分析记录做事后验证，复用 shortterm.history.evaluate_call：
- stock/follow:  T+3 判定（买入 T+3 收益>0 对；卖出 <0 对；观望/未解析不评分）
- pick/screen:   扫描推荐无方向评分，只展示 T+1/T+3/T+10 收益
- deep_review:   评级→方向（Buy/Overweight→买入，Sell/Underweight→卖出）→ 同短线判定
- mainline:      从终态抽 5 档评级→方向；目标价/止损位从决策文本提取后同路径扫描

幂等：同 asof 已有非「待验证」结论 → 跳过（不重算）；force 强制重跑。
落盘: <registry>/validate/<record_id(冒号→下划线)>.json
同步: 索引记录 validation 字段一并更新（update_record）。

安全边界（同 registry）:
- 历史文件只读；K 线缺失/解析失败单条降级为「验证失败/待验证」，不抛。
- 基准收盘价缺省时从 K 线补齐（仅内存副本，不回写原文）。
"""

from __future__ import annotations

import copy
import json
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Optional

from .registry import (
    KINDS,
    _extract_rating_from_state,
    _picks_from_report,
    direction_from_rating,
    query,
    registry_dir,
    update_record,
)

# 最短验证窗口（交易日）：stock/follow T+3 可判定；screen/pick/deep_review/mainline 等 T+10。
_WINDOWS = {
    "stock": 3, "follow": 3, "screen": 10, "pick": 10,
    "deep_review": 10, "mainline": 10,
}

_LOCK = threading.Lock()


def plan_validation_window(rec: dict) -> int:
    """该记录最短验证窗口（交易日）。kind 不在表 → 10。"""
    return _WINDOWS.get(rec.get("kind"), 10)


def _validate_path(record_id_: str) -> Path:
    return registry_dir() / "validate" / (record_id_.replace(":", "_") + ".json")


def _atomic_write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent,
        prefix=path.stem + ".", suffix=".tmp", delete=False,
    ) as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
        tmp = Path(f.name)
    tmp.replace(path)


def load_validation(record_id_: str) -> Optional[dict]:
    """读取单条记录已落盘的验证结果；不存在/损坏 → None。"""
    p = _validate_path(record_id_)
    if not p.exists():
        return None
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _load_full(rec: dict) -> dict:
    """读记录原文（path）；缺失/损坏 → {}（单条降级不抛）。"""
    p = rec.get("path") or ""
    if not p:
        return {}
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _entry_close_from_df(df, rec_date: str) -> Optional[float]:
    """记录日（含）之前的最近收盘价，作为基准兜底。"""
    try:
        prior = df[df["Date"] <= rec_date]
        if prior.empty:
            return None
        return float(prior.iloc[-1]["Close"])
    except Exception:
        return None


def _ensure_entry_close(rec: dict, df) -> dict:
    """ch0.metrics.last_close 缺省 → 用记录日收盘补齐（副本，不回写原文）。"""
    cur = (rec.get("ch0") or {}).get("metrics", {}).get("last_close")
    if cur is not None:
        return rec
    rc = copy.deepcopy(rec)
    close = _entry_close_from_df(df, rec.get("trade_date"))
    if close is None:
        return rc
    rc.setdefault("ch0", {})["metrics"] = {"last_close": close}
    return rc


# ---------------------------------------------------------------------------
# 各类别的评估器（返回 evaluate_call 风格 ev dict）
# ---------------------------------------------------------------------------

def _eval_ev(ticker: str, trade_date: str, parsed: dict, levels: dict,
             asof: str, df) -> dict:
    """构造评估输入并跑 evaluate_call。"""
    from tradingagents.shortterm.history import evaluate_call

    rc = {
        "ticker": ticker, "trade_date": trade_date,
        "parsed": parsed or {}, "levels": levels or {},
        "ch0": {"metrics": {}},
    }
    rc = _ensure_entry_close(rc, df)
    return evaluate_call(rc, asof, df)


def _payload_from_ev(rec: dict, asof: str, ev: dict) -> dict:
    return {
        "record_id": rec.get("id", ""),
        "kind": rec.get("kind", ""),
        "ticker": rec.get("ticker", ""),
        "trade_date": rec.get("trade_date", ""),
        "asof": asof,
        "evaluated_at": int(time.time()),
        "verdict": ev.get("verdict"),
        "verdict_basis": ev.get("verdict_basis", ""),
        "entry_close": ev.get("entry_close"),
        "t1_open_pct": ev.get("t1_open_pct"),
        "t1_close_pct": ev.get("t1_close_pct"),
        "t3_close_pct": ev.get("t3_close_pct"),
        "t10_close_pct": ev.get("t10_close_pct"),
        "bars_after": ev.get("bars_after"),
        "hit_first": ev.get("hit_first"),
        "hit_pct": ev.get("hit_pct"),
        "raw_stop": ev.get("raw_stop"),
        "raw_target": ev.get("raw_target"),
    }


def _eval_shortterm(rec: dict, asof: str, df) -> dict:
    """stock/follow/pick：原文即决策卡结构。"""
    full = _load_full(rec) or rec
    return _eval_ev(
        rec.get("ticker") or full.get("ticker") or "",
        rec.get("trade_date") or "",
        full.get("parsed") or {}, full.get("levels") or {}, asof, df,
    )


def _eval_deep_review(rec: dict, asof: str, df) -> dict:
    """deep_review：signal(5档评级)→方向；报告文本提目标价/止损位。"""
    from tradingagents.shortterm.history import parse_price_levels

    full = _load_full(rec)
    signal = str(full.get("signal") or "")
    direction = direction_from_rating(signal) if signal else None
    return _eval_ev(
        rec.get("ticker") or full.get("ticker") or "",
        rec.get("trade_date") or "",
        {"direction": direction},
        parse_price_levels(full.get("report") or ""), asof, df,
    )


def _eval_mainline(rec: dict, asof: str, df) -> dict:
    """mainline：终态抽评级→方向；决策文本提目标价/止损位。"""
    from tradingagents.shortterm.history import parse_price_levels

    full = _load_full(rec)
    rating = _extract_rating_from_state(full)
    direction = direction_from_rating(rating) if rating != "N/A" else None
    text = ""
    for field in ("final_trade_decision", "trader_investment_decision",
                  "investment_plan"):
        t = full.get(field)
        if t:
            text = t
            break
    return _eval_ev(
        rec.get("ticker") or full.get("ticker") or "",
        rec.get("trade_date") or "",
        {"direction": direction},
        parse_price_levels(text), asof, df,
    )


# ---------------------------------------------------------------------------
# 对外 API
# ---------------------------------------------------------------------------

def validate_record(rec: dict, asof: Optional[str] = None,
                    force: bool = False) -> dict:
    """对单条注册表记录执行事后验证（幂等）。

    跳过条件：已有落盘结果 且 asof >= 本次 asof 且 verdict 非「待验证」。
    force=True 强制重算。任何异常降级为「验证失败」payload，不抛出。
    """
    rid = rec.get("id", "")
    asof = asof or time.strftime("%Y-%m-%d")
    prev = load_validation(rid) if rid else None
    if prev and not force:
        prev_v = prev.get("verdict")
        if prev_v not in (None, "待验证") and str(prev.get("asof") or "") >= asof:
            return prev

    from tradingagents.dataflows.a_stock import _load_ohlcv_astock

    ticker = rec.get("ticker") or ""
    try:
        df = _load_ohlcv_astock(ticker, asof) if ticker else None
        kind = rec.get("kind")
        if kind == "screen":
            payload = _validate_screen(rec, asof)
        elif kind in ("stock", "follow", "pick"):
            payload = _payload_from_ev(rec, asof, _eval_shortterm(rec, asof, df))
        elif kind == "deep_review":
            payload = _payload_from_ev(rec, asof, _eval_deep_review(rec, asof, df))
        elif kind == "mainline":
            payload = _payload_from_ev(rec, asof, _eval_mainline(rec, asof, df))
        else:
            payload = _payload_from_ev(rec, asof, {
                "verdict": "跳过", "verdict_basis": f"kind={kind} 不在验证范围",
            })
    except Exception as e:  # 网络/K线/解析异常 → 降级
        payload = _payload_from_ev(rec, asof, {
            "verdict": "验证失败", "verdict_basis": str(e)[:200],
        })

    if rid:
        with _LOCK:
            _atomic_write(_validate_path(rid), payload)
        update_record(rid, validation=payload)
    return payload


def _validate_screen(rec: dict, asof: str) -> dict:
    """screen：对推荐 TOP N 逐票评估（无方向评分，只展示收益）。"""
    full = _load_full(rec)
    codes = _picks_from_report(full.get("report") or "")
    evs = []
    for code in codes:
        sub = {
            "kind": "pick", "ticker": code, "trade_date": rec.get("trade_date", ""),
            "path": rec.get("path", ""), "id": "",
        }
        try:
            from tradingagents.dataflows.a_stock import _load_ohlcv_astock
            df = _load_ohlcv_astock(code, asof)
            ev = _eval_shortterm(sub, asof, df)
            evs.append({
                "ticker": code, "verdict": ev.get("verdict"),
                "t1_close_pct": ev.get("t1_close_pct"),
                "t3_close_pct": ev.get("t3_close_pct"),
                "t10_close_pct": ev.get("t10_close_pct"),
                "bars_after": ev.get("bars_after"),
                "hit_first": ev.get("hit_first"), "hit_pct": ev.get("hit_pct"),
            })
        except Exception:
            continue
    base = _payload_from_ev(rec, asof, {
        "verdict": "多票汇总", "verdict_basis": f"TOP {len(codes)} 逐票收益",
    })
    base["picks"] = evs
    return base


def run_validations(ticker: Optional[str] = None, kind: Optional[str] = None,
                    asof: Optional[str] = None, force: bool = False) -> list[dict]:
    """注册表全部/过滤记录统一验证。单条失败跳过，不中断。"""
    out = []
    for rec in query(ticker=ticker, kind=kind):
        try:
            out.append(validate_record(rec, asof=asof, force=force))
        except Exception:
            continue
    return out


__all__ = [
    "KINDS", "load_validation", "plan_validation_window",
    "run_validations", "validate_record",
]
