"""执行轨迹落盘：Prompt 全文 + LLM 原始返回 + 校验结果 + 耗时，按需开启。

开启方式（默认关闭）:
  - pipeline.run(..., trace=True)（web UI 勾选"记录执行轨迹"）
  - 环境变量 ST_TRACE=1（全局）

存储: ~/.tradingagents/shortterm/traces/<ticker>_<trade_date>_<ts>.json
清理: 每次写入时顺带删除 mtime 超过 90 天的旧轨迹。
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from pathlib import Path

_DIR = Path.home() / ".tradingagents" / "shortterm" / "traces"
_LOCK = threading.Lock()
_MAX_AGE_DAYS = 90


def trace_enabled(flag: bool = False) -> bool:
    return flag or os.environ.get("ST_TRACE") == "1"


def _cleanup_old(now: float) -> None:
    cutoff = now - _MAX_AGE_DAYS * 86400
    for p in _DIR.glob("*.json"):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
        except OSError:
            continue


def save_trace(record: dict) -> Path:
    """落盘一条执行轨迹，返回路径。顺带清理 90 天前旧文件。"""
    _DIR.mkdir(parents=True, exist_ok=True)
    now = time.time()
    payload = dict(record)
    payload["ts"] = int(now)
    path = _DIR / f"{record.get('ticker', 'unknown')}_{record.get('trade_date', 'nodate')}_{int(now)}.json"
    with _LOCK:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=_DIR,
            prefix=path.stem + ".", suffix=".tmp", delete=False,
        ) as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)
            tmp = Path(f.name)
        tmp.replace(path)
        _cleanup_old(now)
    return path


def list_traces(ticker: str | None = None) -> list[dict]:
    """扫描轨迹目录，返回摘要列表（新→旧）。摘要不含 prompt/response 大字段。"""
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
        out.append({
            "path": str(p),
            "ticker": r.get("ticker"),
            "name": r.get("name", ""),
            "trade_date": r.get("trade_date"),
            "mode": r.get("mode"),
            "ts": r.get("ts", 0),
            "attempts": r.get("attempts", 1),
            "elapsed_ms": r.get("elapsed_ms"),
            "ok": (r.get("validation") or {}).get("ok"),
        })
    # 稳定排序：先按路径倒序（同秒 tiebreak），再按 ts 倒序（主键）
    out.sort(key=lambda e: e["path"], reverse=True)
    out.sort(key=lambda e: -e["ts"])
    return out


def load_trace(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)
