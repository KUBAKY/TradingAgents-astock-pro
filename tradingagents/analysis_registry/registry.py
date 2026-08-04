"""统一分析结果注册表（全方式关联索引）。

背景: 短线决策 / 主线分析 / 深度复核 / 扫描选股 / 持仓跟进 各自落盘在不同目录，
历史记录互不可见，无法做「同票纵向对比」与「事后验证闭环」。
本模块只读历史文件、把每条分析摘要登记进单一索引，供对比/验证/复盘使用。

存储: ~/.tradingagents/analysis_registry/（可用 TRADINGAGENTS_REGISTRY_DIR 覆盖）
  index.json  — {version, records: [...]}，record_id = <kind>:<ticker>:<trade_date>:<ts>
  compare/    — 对比报告（compare.py 写入）
  validate/   — 验证结果（validate.py 写入）
  reports/    — 误差分析报告（feedback.py 写入）

kind: stock(短线个股) / follow(持仓跟进) / screen(扫描) / pick(扫描推荐个股)
      / deep_review(主线深度复核) / mainline(主线分析)

安全边界:
- 历史文件只读，零迁移零修改。
- 任何读取/解析失败单条跳过；register/backfill 内部异常不抛出（调用方免 try）。
- 幂等：record_id 已存在则跳过，重复 backfill / 重复 register 安全。
- 索引写入原子（临时文件 + replace），threading.Lock 串行。
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Optional

_DIR_ENV = "TRADINGAGENTS_REGISTRY_DIR"
# 只读数据源目录（测试用 env 指向临时目录；生产留空走默认路径）
_SHORTTERM_DIR_ENV = "TRADINGAGENTS_SHORTTERM_DIR"
_LOGS_DIR_ENV = "TRADINGAGENTS_LOGS_DIR"
_DEEPREVIEW_DIR_ENV = "TRADINGAGENTS_DEEPREVIEW_DIR"

KINDS = ("stock", "follow", "screen", "pick", "deep_review", "mainline")

_LOCK = threading.Lock()

_MAINLINE_LOG_RE = re.compile(r"full_states_log_(\d{4}-\d{2}-\d{2})\.json$")

_RATING_TO_DIRECTION = {
    "Buy": "买入", "Overweight": "买入",
    "Hold": "观望",
    "Underweight": "卖出", "Sell": "卖出",
}


def registry_dir() -> Path:
    env = os.environ.get(_DIR_ENV)
    if env:
        return Path(env)
    return Path.home() / ".tradingagents" / "analysis_registry"


def _shortterm_dir() -> Path:
    env = os.environ.get(_SHORTTERM_DIR_ENV)
    if env:
        return Path(env)
    return Path.home() / ".tradingagents" / "shortterm"


def _logs_dir() -> Path:
    env = os.environ.get(_LOGS_DIR_ENV)
    if env:
        return Path(env)
    return Path.home() / ".tradingagents" / "logs"


def _deepreview_dir() -> Path:
    env = os.environ.get(_DEEPREVIEW_DIR_ENV)
    if env:
        return Path(env)
    return Path.home() / ".tradingagents" / "shortterm" / "deepreview"


def _index_path() -> Path:
    return registry_dir() / "index.json"


def _atomic_write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent,
        prefix=path.stem + ".", suffix=".tmp", delete=False,
    ) as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
        tmp = Path(f.name)
    tmp.replace(path)


def _load_records() -> list[dict]:
    """读 index.json 记录列表；文件缺失/损坏 → 空列表。"""
    p = _index_path()
    if not p.exists():
        return []
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []
    records = data.get("records") if isinstance(data, dict) else None
    return records if isinstance(records, list) else []


def _save_records(records: list[dict]) -> None:
    _atomic_write(_index_path(), {"version": 1, "records": records})


def record_id(kind: str, ticker: str, trade_date: str, ts: int) -> str:
    return f"{kind}:{ticker}:{trade_date}:{int(ts)}"


def direction_from_rating(rating: Optional[str]) -> Optional[str]:
    """主线 5 档评级 → 短线方向词（Buy/Overweight→买入，Sell/Underweight→卖出，Hold→观望）。"""
    if not rating:
        return None
    return _RATING_TO_DIRECTION.get(rating)


# ---------------------------------------------------------------------------
# 摘要构建（各来源字段不同，统一收敛为扁平 summary）
# ---------------------------------------------------------------------------

def _summary_for_stock(r: dict) -> dict:
    ch0 = r.get("ch0") or {}
    parsed = r.get("parsed") or {}
    levels = r.get("levels") or {}
    metrics = ch0.get("metrics") or {}
    return {
        "direction": parsed.get("direction"),
        "confidence": parsed.get("confidence"),
        "horizon": parsed.get("horizon"),
        "target_raw": levels.get("raw_target", "")[:120],
        "stop_raw": levels.get("raw_stop", "")[:120],
        "last_close": metrics.get("last_close"),
        "anomaly_types": [a.get("type") for a in (ch0.get("anomalies") or [])],
        "mode": r.get("mode"),
    }


def _summary_for_screen(r: dict) -> dict:
    scan = r.get("scan") or {}
    sentiment = scan.get("market_sentiment") or {}
    return {
        "capital": r.get("capital"),
        "sentiment": sentiment.get("label"),
        "limit_up_count": sentiment.get("limit_up_count"),
        "max_streak": sentiment.get("max_streak"),
    }


def _summary_for_deep_review(r: dict) -> dict:
    return {
        "rating": r.get("signal"),
        "direction": direction_from_rating(r.get("signal")),
        "reason": (r.get("reason") or "")[:200],
    }


def _extract_rating_from_state(state: dict) -> str:
    """从主线终态提取 5 档评级（与 web.history.extract_signal 同启发式）。

    独立实现避免 tradingagents 包反向依赖 web 包。
    """
    from tradingagents.agents.utils.rating import parse_rating

    _UNKNOWN = ""
    for field in (
        "final_trade_decision",
        "trader_investment_decision",
        "investment_plan",
    ):
        text = state.get(field, "")
        if not text:
            continue
        cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
        rating = parse_rating(cleaned, default=_UNKNOWN)
        if rating:
            return rating
    return "N/A"


def _summary_for_mainline(r: dict) -> dict:
    rating = _extract_rating_from_state(r)
    return {
        "rating": rating,
        "direction": direction_from_rating(rating) if rating != "N/A" else None,
    }


# ---------------------------------------------------------------------------
# 回填：首次建索引时扫描全部历史文件（只读、幂等）
# ---------------------------------------------------------------------------

def _iter_shortterm(records: list[dict], ids: set[str]) -> int:
    """shortterm 目录: 个股/持仓跟进 + 扫描 + 扫描推荐。返回新增条数。"""
    added = 0
    d = _shortterm_dir()
    if not d.exists():
        return 0
    for p in d.glob("*.json"):
        try:
            with open(p, encoding="utf-8") as f:
                r = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        kind = r.get("kind")
        if kind == "stock":
            effective = ("follow" if (r.get("inputs") or {}).get("kind") == "follow"
                         else "stock")
            rec = {
                "id": record_id(effective, r.get("ticker", ""), r.get("trade_date", ""),
                                 r.get("ts", 0)),
                "kind": effective, "ticker": r.get("ticker", ""),
                "name": r.get("name", ""), "trade_date": r.get("trade_date", ""),
                "ts": int(r.get("ts", 0) or 0), "path": str(p),
                "summary": _summary_for_stock(r), "validation": None,
                "registered_at": int(time.time()),
            }
            if rec["id"] not in ids:
                records.append(rec)
                ids.add(rec["id"])
                added += 1
        elif kind == "screen":
            rec = {
                "id": record_id("screen", "", r.get("trade_date", ""), r.get("ts", 0)),
                "kind": "screen", "ticker": "", "name": "",
                "trade_date": r.get("trade_date", ""), "ts": int(r.get("ts", 0) or 0),
                "path": str(p), "summary": _summary_for_screen(r),
                "validation": None, "registered_at": int(time.time()),
            }
            if rec["id"] not in ids:
                records.append(rec)
                ids.add(rec["id"])
                added += 1
            for i, code in enumerate(_picks_from_report(r.get("report", ""))):
                pick = {
                    "id": record_id("pick", code, r.get("trade_date", ""), r.get("ts", 0)),
                    "kind": "pick", "ticker": code, "name": "",
                    "trade_date": r.get("trade_date", ""),
                    "ts": int(r.get("ts", 0) or 0), "path": str(p),
                    "summary": {"rank": i + 1, "screen_date": r.get("trade_date", "")},
                    "validation": None, "registered_at": int(time.time()),
                }
                if pick["id"] not in ids:
                    records.append(pick)
                    ids.add(pick["id"])
                    added += 1
    return added


_PICK_RE = re.compile(
    r"(?<!\d)(?:00[0132]\d{3}|30[012]\d{3}|60[0135]\d{3}|68[89]\d{3}"
    r"|82\d{4}|83\d{4}|87\d{4}|88\d{4}|43\d{4}|92\d{4})(?![\d.])")


def _picks_from_report(report_text: str, n: int = 3) -> list[str]:
    """从扫描推荐报告提取 TOP N 代码（同 screener.extract_picks 规则，去重）。"""
    seen: list[str] = []
    for code in _PICK_RE.findall(report_text or ""):
        if code not in seen:
            seen.append(code)
        if len(seen) >= n:
            break
    return seen


def _iter_deepreview(records: list[dict], ids: set[str]) -> int:
    added = 0
    d = _deepreview_dir()
    if not d.exists():
        return 0
    for p in d.glob("*.json"):
        try:
            with open(p, encoding="utf-8") as f:
                r = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        rec = {
            "id": record_id("deep_review", r.get("ticker", ""), r.get("trade_date", ""),
                            r.get("ts", 0)),
            "kind": "deep_review", "ticker": r.get("ticker", ""), "name": "",
            "trade_date": r.get("trade_date", ""),
            "ts": int(r.get("ts", 0) or 0), "path": str(p),
            "summary": _summary_for_deep_review(r), "validation": None,
            "registered_at": int(time.time()),
        }
        if rec["id"] not in ids:
            records.append(rec)
            ids.add(rec["id"])
            added += 1
    return added


def _iter_mainline(records: list[dict], ids: set[str]) -> int:
    added = 0
    root = _logs_dir()
    if not root.exists():
        return 0
    for p in root.rglob("full_states_log_*.json"):
        m = _MAINLINE_LOG_RE.search(p.name)
        if not m:
            continue
        trade_date = m.group(1)
        ticker = p.parent.parent.name
        try:
            with open(p, encoding="utf-8") as f:
                state = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        ts = int(p.stat().st_mtime) if p.exists() else 0
        rec = {
            "id": record_id("mainline", ticker, trade_date, ts),
            "kind": "mainline", "ticker": ticker, "name": "",
            "trade_date": trade_date, "ts": ts, "path": str(p),
            "summary": _summary_for_mainline(state), "validation": None,
            "registered_at": int(time.time()),
        }
        if rec["id"] not in ids:
            records.append(rec)
            ids.add(rec["id"])
            added += 1
    return added


def backfill_index() -> dict:
    """扫描全部历史文件回填索引。幂等（已有 record_id 跳过）。返回各来源新增数。

    不抛异常：目录缺失/文件损坏均跳过。
    """
    with _LOCK:
        records = _load_records()
        ids = {r.get("id") for r in records if r.get("id")}
        counts = {
            "stock_follow": _iter_shortterm(records, ids),
            "deep_review": _iter_deepreview(records, ids),
            "mainline": _iter_mainline(records, ids),
        }
        if records:
            _save_records(records)
        counts["total"] = len(records)
        return counts


def _index_exists() -> bool:
    return _index_path().exists()


# ---------------------------------------------------------------------------
# 对外 API
# ---------------------------------------------------------------------------

def register(
    kind: str,
    ticker: str,
    trade_date: str,
    ts: Optional[float | int] = None,
    *,
    path: str,
    name: str = "",
    summary: Optional[dict] = None,
    validation: Optional[dict] = None,
) -> Optional[str]:
    """登记一条分析结果。幂等：同 record_id 已存在 → 跳过，仍返回该 id。

    参数缺失（无 ticker 且 kind 需要 ticker / 无 trade_date / 无 path）→ 返回 None。
    任何异常内部吞掉并返回 None（调用方免 try，不影响主流程）。
    """
    try:
        if kind not in KINDS:
            return None
        ticker = str(ticker or "").strip()
        trade_date = str(trade_date or "").strip()
        if not trade_date or not path:
            return None
        if kind in ("stock", "follow", "pick", "deep_review", "mainline") and not ticker:
            return None
        ts = int(ts if ts is not None else time.time())
        rid = record_id(kind, ticker, trade_date, ts)
        if not _index_exists():
            backfill_index()
        rec = {
            "id": rid, "kind": kind, "ticker": ticker, "name": str(name or ""),
            "trade_date": trade_date, "ts": ts, "path": str(path),
            "summary": summary or {}, "validation": validation,
            "registered_at": int(time.time()),
        }
        with _LOCK:
            records = _load_records()
            if any(r.get("id") == rid for r in records):
                return rid
            records.append(rec)
            _save_records(records)
        try:
            from tradingagents.analysis_registry.compare import after_register
            after_register(kind, ticker)
        except Exception:
            pass
        return rid
    except Exception:
        return None


def update_record(record_id_: str, **fields) -> bool:
    """更新单条记录的字段（如验证结果 validation）。不存在 → False，不抛。"""
    try:
        with _LOCK:
            records = _load_records()
            for r in records:
                if r.get("id") == record_id_:
                    r.update(fields)
                    _save_records(records)
                    return True
        return False
    except Exception:
        return False


def query(
    ticker: Optional[str] = None,
    kind: Optional[str] = None,
    since: Optional[int] = None,
    limit: Optional[int] = None,
) -> list[dict]:
    """查询索引记录（新→旧）。ticker 精确匹配（空串记录永不命中非空 ticker）。"""
    try:
        if not _index_exists():
            backfill_index()
        records = _load_records()
    except Exception:
        return []
    out = []
    for r in records:
        if ticker and r.get("ticker") != str(ticker).strip().upper():
            continue
        if kind and r.get("kind") != kind:
            continue
        if since is not None and int(r.get("ts", 0) or 0) < int(since):
            continue
        out.append(r)
    out.sort(key=lambda r: int(r.get("ts", 0) or 0), reverse=True)
    if limit is not None:
        out = out[: int(limit)]
    return out


def get_stock_timeline(ticker: str) -> list[dict]:
    """单只股票跨方式纵向时间线（旧→新），供对比/复盘。"""
    records = query(ticker=ticker)
    records.sort(key=lambda r: (r.get("trade_date") or "", int(r.get("ts", 0) or 0)))
    return records


# ---------------------------------------------------------------------------
# 各保存点便捷注册（summary 就地构建，调用方免构造）
# ---------------------------------------------------------------------------

def register_stock_record(record: dict, path: str) -> Optional[str]:
    """注册一条短线个股/持仓跟进记录（record=落盘 payload，与 backfill 同源字段）。"""
    kind = ("follow" if (record.get("inputs") or {}).get("kind") == "follow"
            else "stock")
    return register(kind, record.get("ticker", ""), record.get("trade_date", ""),
                    ts=record.get("ts"), path=path, name=record.get("name", ""),
                    summary=_summary_for_stock(record))


def register_screen_record(record: dict, path: str) -> Optional[str]:
    """注册扫描记录 + 其中 TOP N 推荐（picks，去重同 backfill）。"""
    rid = register("screen", "", record.get("trade_date", ""),
                   ts=record.get("ts"), path=path,
                   summary=_summary_for_screen(record))
    for i, code in enumerate(_picks_from_report(record.get("report", ""))):
        register("pick", code, record.get("trade_date", ""),
                 ts=record.get("ts"), path=path,
                 summary={"rank": i + 1, "screen_date": record.get("trade_date", "")})
    return rid


def register_deep_review_record(record: dict, path: str) -> Optional[str]:
    """注册一条主线深度复核记录。"""
    return register("deep_review", record.get("ticker", ""),
                    record.get("trade_date", ""), ts=record.get("ts"), path=path,
                    summary=_summary_for_deep_review(record))


def register_mainline_record(ticker: str, trade_date: str, path: str,
                             state: dict) -> Optional[str]:
    """注册一条主线分析记录（ts 取日志文件 mtime，终态 state 提炼评级）。"""
    try:
        ts = int(Path(path).stat().st_mtime)
    except OSError:
        ts = int(time.time())
    return register("mainline", ticker, trade_date, ts=ts, path=path,
                    summary=_summary_for_mainline(state))
