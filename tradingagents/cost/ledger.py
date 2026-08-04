"""AI 成本账本：~/.tradingagents/cost/ledger.jsonl，每行一次 LLM 调用。

- 追加线程安全（进程内锁）
- 超 90 天旧行自动归档到 ledger_archive.jsonl（不删，可回溯）
- summarize(period) 聚合今日/本周/本月
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from . import pricing

_LOCK = threading.Lock()
_ARCHIVE_TTL_DAYS = 90


def cost_dir() -> Path:
    env = os.environ.get("TRADINGAGENTS_COST_DIR")
    if env:
        return Path(env)
    return Path.home() / ".tradingagents" / "cost"


def ledger_path() -> Path:
    return cost_dir() / "ledger.jsonl"


def archive_path() -> Path:
    return cost_dir() / "ledger_archive.jsonl"


def append_entry(entry: dict) -> None:
    """追加一行账目。自动补 ts，顺带轮转归档超 90 天的旧行。"""
    entry = dict(entry)
    entry.setdefault("ts", int(time.time()))
    path = ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        _rotate_if_needed(path)


def _rotate_if_needed(path: Path) -> None:
    if not path.exists():
        return
    cutoff = time.time() - _ARCHIVE_TTL_DAYS * 86400
    old, fresh = [], []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
                if row.get("ts", 0) < cutoff:
                    old.append(row)
                    continue
            except json.JSONDecodeError:
                pass
            fresh.append(line)
    except OSError:
        return
    if not old:
        return
    if fresh:
        path.write_text("\n".join(fresh) + "\n", encoding="utf-8")
    else:
        path.unlink()
    with open(archive_path(), "a", encoding="utf-8") as f:
        for row in old:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def read_entries() -> list[dict]:
    path = ledger_path()
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def aggregate_entries(entries: list[dict]) -> dict:
    """聚合一批账目：成本/总token/未定价token/调用次数。"""
    cost = 0.0
    tokens = 0
    unpriced = 0
    for e in entries:
        t = int(e.get("input") or 0) + int(e.get("output") or 0)
        tokens += t
        c = e.get("cost_cny")
        if c is None:
            unpriced += t
        else:
            cost += c
    return {
        "cost_cny": cost,
        "tokens": tokens,
        "unpriced_tokens": unpriced,
        "calls": len(entries),
    }


def recent(feature: str | None = None, run_id: str | None = None,
           limit: int = 10, since_ts: float | None = None) -> list[dict]:
    ents = read_entries()
    if since_ts is not None:
        ents = [e for e in ents if e.get("ts", 0) >= since_ts]
    if feature:
        ents = [e for e in ents if e.get("feature") == feature]
    if run_id:
        ents = [e for e in ents if e.get("run_id") == run_id]
    return ents[-limit:]


def summarize(period: str = "today", now: datetime | None = None) -> dict:
    """period: today | week | month（按本地时区日历）。now 可注入（测试用）。"""
    now = now or datetime.now()
    if period == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "week":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=now.weekday())
    elif period == "month":
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    else:
        raise ValueError(f"未知周期: {period}")

    ents = [e for e in read_entries() if e.get("ts", 0) >= start.timestamp()]
    by_feature: dict[str, list[dict]] = {}
    for e in ents:
        by_feature.setdefault(e.get("feature", "?"), []).append(e)

    rows = sorted(
        ({"feature": f, **aggregate_entries(es)} for f, es in by_feature.items()),
        key=lambda r: -r["cost_cny"],
    )
    return {
        "period": period,
        "rows": rows,
        "calls": sum(r["calls"] for r in rows),
        "total_cost_cny": round(sum(r["cost_cny"] for r in rows), 4),
        "total_tokens": sum(r["tokens"] for r in rows),
        "pricing": pricing.pricing_info(),
    }
