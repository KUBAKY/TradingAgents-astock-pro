"""短线 ↔ 主线深度复核桥梁（#2 持仓异常 / #4 扫描 TOP N 共用）。

run_deep_review: 调主线 TradingAgentsGraph 全链路分析（7 analyst 辩论，
单票 15-20 分钟），把最终报告 + 5 档评级落盘
~/.tradingagents/shortterm/deepreview/（可用 TRADINGAGENTS_DEEPREVIEW_DIR 覆盖）。

- lazy import trading_graph：防 graph→shortterm 循环依赖。
- 任何异常返回 {"ok": False, "error": ...}，不抛给调用方。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_DIR_ENV = "TRADINGAGENTS_DEEPREVIEW_DIR"
_LOCK = threading.Lock()


def deep_review_dir() -> Path:
    env = os.environ.get(_DIR_ENV)
    if env:
        return Path(env)
    return Path.home() / ".tradingagents" / "shortterm" / "deepreview"


def _atomic_write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent,
        prefix=path.stem + ".", suffix=".tmp", delete=False,
    ) as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
        tmp = Path(f.name)
    tmp.replace(path)


def run_deep_review(
    ticker: str,
    trade_date: str,
    reason: str = "",
    config: Optional[dict] = None,
) -> dict:
    """对单票跑主线深度分析并落盘。

    Args:
        ticker: 6 位代码或中文名（propagation 内部做解析）。
        trade_date: YYYY-MM-DD。
        reason: 触发原因（#2 持仓异常 / #4 扫描 TOP N），写入落盘文件。
        config: 主线 config；None 用 DEFAULT_CONFIG。
    Returns:
        {"ok": True, ticker, trade_date, reason, signal, report,
         report_path, cost_summary}
        失败: {"ok": False, ticker, trade_date, reason, error}
    """
    from tradingagents.graph.trading_graph import TradingAgentsGraph

    if config is None:
        from tradingagents.default_config import DEFAULT_CONFIG
        config = DEFAULT_CONFIG

    ts = time.time()
    base = {
        "kind": "deep_review",
        "ticker": str(ticker),
        "trade_date": str(trade_date),
        "reason": reason,
        "ts": ts,
    }
    try:
        graph = TradingAgentsGraph(debug=False, config=config)
        final_state, signal = graph.propagate(ticker, trade_date)

        report = (final_state or {}).get("final_trade_decision", "")
        payload = {
            **base,
            "signal": str(signal),
            "report": report,
            "analyst_reports": {
                k: (final_state or {}).get(k, "")
                for k in ("market_report", "sentiment_report", "news_report",
                          "fundamentals_report", "policy_report",
                          "hot_money_report", "lockup_report",
                          "data_quality_summary", "investment_plan",
                          "trader_investment_plan")
            },
        }
        path = deep_review_dir() / f"{ticker}_{trade_date}_{int(ts * 1000)}.json"
        with _LOCK:
            _atomic_write(path, payload)
        try:
            from tradingagents.analysis_registry.registry import (
                register_deep_review_record,
            )
            register_deep_review_record(payload, str(path))
        except Exception:
            pass
        logger.info("deep_review %s %s -> signal=%s path=%s",
                    ticker, trade_date, signal, path)
        return {**base, "ok": True, "signal": str(signal),
                "report": report, "report_path": str(path)}
    except Exception as e:
        logger.exception("deep_review failed for %s %s: %s", ticker, trade_date, e)
        return {**base, "ok": False, "error": str(e)}


def load_deep_reviews(ticker: Optional[str] = None,
                      limit: int = 20) -> list[dict]:
    """读取历史深度复核记录（最新在前）；可按 ticker 过滤。"""
    d = deep_review_dir()
    if not d.exists():
        return []
    out = []
    for p in d.glob("*.json"):
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if ticker and str(rec.get("ticker")) != str(ticker):
            continue
        rec["path"] = str(p)
        out.append(rec)
    out.sort(key=lambda r: r.get("ts", 0), reverse=True)
    return out[:limit]
