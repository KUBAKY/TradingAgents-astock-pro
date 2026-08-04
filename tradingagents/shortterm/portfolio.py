"""持仓管理：CRUD + 每日收盘跟进 + 盈亏快照。

存储: ~/.tradingagents/shortterm/portfolio/（可用 TRADINGAGENTS_PORTFOLIO_DIR 覆盖）
  state.json            持仓列表（个人数据，不入库）
  snapshots/<date>.json 每日快照（现价/盈亏 + 跟进结论）

run_daily_follow(date)：对每只持仓跑 pipeline.run（带 cost/shares → 割/持/补决策卡），
幂等：当日快照已存在则不重跑（force=True 强制重跑）；单只失败不阻塞其余。
"""

from __future__ import annotations

import json
import re
import tempfile
import threading
import time
from datetime import date as _date
from pathlib import Path
from typing import Any, Optional

from .ch0 import run_ch0
from .history import parse_decision, save_stock_record

_DIR_ENV = "TRADINGAGENTS_PORTFOLIO_DIR"
_LOCK = threading.Lock()


def portfolio_dir() -> Path:
    env = __import__("os").environ.get(_DIR_ENV)
    if env:
        return Path(env)
    return Path.home() / ".tradingagents" / "shortterm" / "portfolio"


def _state_path() -> Path:
    return portfolio_dir() / "state.json"


def _snapshot_dir() -> Path:
    return portfolio_dir() / "snapshots"


def _atomic_write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent,
        prefix=path.stem + ".", suffix=".tmp", delete=False,
    ) as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
        tmp = Path(f.name)
    tmp.replace(path)


def _load_positions() -> list[dict]:
    path = _state_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        positions = data.get("positions", []) if isinstance(data, dict) else []
        return [p for p in positions if isinstance(p, dict) and p.get("ticker")]
    except (OSError, json.JSONDecodeError):
        return []


def _save_positions(positions: list[dict]) -> None:
    with _LOCK:
        _atomic_write(_state_path(), {"positions": positions, "updated_ts": int(time.time())})


def _lookup_name(ticker: str) -> str:
    """代码 → 名称（mootdx 全市场映射，有缓存）。失败静默返回空串。"""
    try:
        from tradingagents.dataflows.a_stock import _build_name_code_map
        _, c2n = _build_name_code_map()
        return c2n.get(ticker, "")
    except Exception:
        return ""


_TICKER_RE = re.compile(r"^[036]\d{5}$")


def list_positions() -> list[dict]:
    """持仓列表（保持添加顺序）。"""
    return _load_positions()


def add_position(ticker: str, cost_price: float, shares: int,
                 buy_date: Optional[str] = None, note: str = "") -> dict:
    """新增持仓。校验：6位代码 / 成本>0 / 股数>0 / 代码不重复。"""
    ticker = ticker.strip()
    if not _TICKER_RE.match(ticker):
        raise ValueError(f"非法股票代码: {ticker}")
    if cost_price is None or cost_price <= 0:
        raise ValueError("成本价必须 > 0")
    if shares is None or shares <= 0:
        raise ValueError("股数必须 > 0")

    positions = _load_positions()
    if any(p["ticker"] == ticker for p in positions):
        raise ValueError(f"已存在持仓: {ticker}（请用 update_position 修改）")

    try:
        name = _lookup_name(ticker)
    except Exception:
        name = ""  # 名称查询（mootdx）失败绝不阻塞添加

    pos = {
        "ticker": ticker,
        "name": name,
        "cost_price": float(cost_price),
        "shares": int(shares),
        "buy_date": buy_date or _date.today().isoformat(),
        "note": note or "",
        "created_ts": int(time.time()),
        "updated_ts": int(time.time()),
    }
    positions.append(pos)
    _save_positions(positions)
    return pos


def update_position(ticker: str, **fields) -> dict:
    """部分更新持仓字段（cost_price/shares/buy_date/note/name）。字段不存在 → ValueError。"""
    positions = _load_positions()
    for p in positions:
        if p["ticker"] == ticker:
            allowed = {"cost_price", "shares", "buy_date", "note", "name"}
            unknown = set(fields) - allowed
            if unknown:
                raise ValueError(f"未知字段: {sorted(unknown)}")
            if "cost_price" in fields:
                if fields["cost_price"] is None or fields["cost_price"] <= 0:
                    raise ValueError("成本价必须 > 0")
                fields["cost_price"] = float(fields["cost_price"])
            if "shares" in fields:
                if fields["shares"] is None or fields["shares"] <= 0:
                    raise ValueError("股数必须 > 0")
                fields["shares"] = int(fields["shares"])
            p.update(fields)
            p["updated_ts"] = int(time.time())
            _save_positions(positions)
            return p
    raise ValueError(f"持仓不存在: {ticker}")


def remove_position(ticker: str) -> bool:
    """删除持仓。存在 → True，不存在 → False。"""
    positions = _load_positions()
    kept = [p for p in positions if p["ticker"] != ticker]
    if len(kept) == len(positions):
        return False
    _save_positions(kept)
    return True


# ---------------------------------------------------------------------------
# 每日快照（现价/盈亏）
# ---------------------------------------------------------------------------

def _quote(ticker: str, trade_date: str) -> dict:
    ch0 = run_ch0(ticker, trade_date)
    return {
        "name": ch0.get("name", ""),
        "last_close": (ch0.get("metrics") or {}).get("last_close"),
    }


def take_snapshot(trade_date: str) -> Path:
    """取全部持仓现价快照并落盘 snapshots/<date>.json。单票行情失败 → 行内 error，不中断。"""
    rows = []
    total_value = total_cost = 0.0
    for p in _load_positions():
        row = {
            "ticker": p["ticker"], "name": p.get("name", ""),
            "cost_price": p["cost_price"], "shares": p["shares"],
            "buy_date": p.get("buy_date"), "note": p.get("note", ""),
            "last_close": None, "market_value": None,
            "pnl": None, "pnl_pct": None, "error": None,
        }
        try:
            q = _quote(p["ticker"], trade_date)
            px = q["last_close"]
            row["name"] = q["name"] or row["name"]
            if px is None:
                row["error"] = "无收盘价"
            else:
                row["last_close"] = float(px)
                row["market_value"] = round(row["last_close"] * p["shares"], 2)
                row["pnl"] = round((row["last_close"] - p["cost_price"]) * p["shares"], 2)
                row["pnl_pct"] = round((row["last_close"] / p["cost_price"] - 1) * 100, 2)
                total_value += row["market_value"]
                total_cost += p["cost_price"] * p["shares"]
        except Exception as e:
            row["error"] = str(e) or "行情获取失败"
        rows.append(row)

    payload = {
        "date": trade_date,
        "ts": int(time.time()),
        "positions": rows,
        "total_value": round(total_value, 2),
        "total_pnl": round(total_value - total_cost, 2),
        "total_cost": round(total_cost, 2),
        "pnl_pct": round((total_value - total_cost) / total_cost * 100, 2) if total_cost else None,
    }
    path = _snapshot_dir() / f"{trade_date}.json"
    with _LOCK:
        _atomic_write(path, payload)
    return path


def load_snapshot(trade_date: str) -> Optional[dict]:
    path = _snapshot_dir() / f"{trade_date}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def list_snapshots() -> list[str]:
    """快照日期列表（新→旧）。"""
    d = _snapshot_dir()
    if not d.exists():
        return []
    out = [p.stem for p in d.glob("*.json")
           if re.match(r"^\d{4}-\d{2}-\d{2}$", p.stem)]
    return sorted(out, reverse=True)


# ---------------------------------------------------------------------------
# 每日跟进（LLM 决策卡，幂等）
# ---------------------------------------------------------------------------

def run_daily_follow(trade_date: str, provider: str = "deepseek",
                     model: str = "deepseek-v4-flash", base_url: Optional[str] = None,
                     force: bool = False, positions: Optional[list[dict]] = None) -> dict:
    """对每只持仓跑完整决策（割/持/补），结果并入快照落盘。

    幂等：当日快照已存在且非 force → 直接返回 {"skipped": True}。
    单票失败 → results 行内 error，继续其余；failed 计数。
    """
    existing = load_snapshot(trade_date)
    if existing is not None and not force:
        return {"date": trade_date, "skipped": True, "snapshot": existing,
                "results": existing.get("results", []), "failed": 0}

    run = _import_pipeline()  # 测试可先 monkeypatch portfolio.pipeline_run 再进入
    results = []
    failed = 0
    for p in positions if positions is not None else _load_positions():
        entry = {
            "ticker": p["ticker"], "name": p.get("name", ""),
            "cost_price": p["cost_price"], "shares": p["shares"],
            "direction": None, "confidence": None,
            "cost_cny": None, "report_path": None, "error": None,
        }
        try:
            result = run(p["ticker"], trade_date,
                         cost=p["cost_price"], shares=p["shares"],
                         provider=provider, model=model,
                         base_url=base_url, trace=False,
                         cost_feature="portfolio")
            parsed = parse_decision(result.get("report", ""))
            entry["direction"] = parsed.get("direction")
            entry["confidence"] = parsed.get("confidence")
            cost_summary = result.get("cost")
            if cost_summary and cost_summary.get("total_cost_cny") is not None:
                entry["cost_cny"] = round(cost_summary["total_cost_cny"], 4)
            path = save_stock_record(result, {"kind": "follow", "trade_date": trade_date})
            entry["report_path"] = str(path) if path else None
        except Exception as e:
            entry["error"] = str(e) or "跟进失败"
            failed += 1
        results.append(entry)

    snap = take_snapshot(trade_date)
    payload = load_snapshot(trade_date) or {}
    payload["results"] = results
    payload["failed"] = failed
    payload["followed_ts"] = int(time.time())
    _atomic_write(snap, payload)
    return {"date": trade_date, "skipped": False, "snapshot": payload,
            "results": results, "failed": failed}


# 模块级别名，便于测试 monkeypatch
pipeline_run = None


def _import_pipeline():
    global pipeline_run
    if pipeline_run is None:
        from .pipeline import run as pipeline_run
    return pipeline_run
