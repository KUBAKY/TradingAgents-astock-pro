"""登记一次体系分析结果，供盘后前向验证（体系 Ch8 的「前向纸面回测」）。

为什么需要：账面回测有幸存者偏差与事后选股，前向验证不需要历史数据——
今天给出结论、落一条记录，10 个交易日后系统自动打分。这是"规则自洽"走向"规则有效"的唯一免费路径。

用法：
  python scripts/register_system_analysis.py \
      --ticker 300308 --date 2026-10-23 --rating Hold \
      --paradigm 价值 --score 55.6 --confidence 低 \
      --note "PB 78% 分位、CFO/净利 0.33、无安全边际"

  # 带目标价与止损（可选；观望结论通常不填）
  ... --target 700 --stop 520

落盘：~/.tradingagents/system_analysis/<ticker>_<date>_<ts>.json
索引：<registry>/index.json 中一条 kind=system 的记录，10 个交易日后由 run_validations.py 自动验证
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tradingagents.analysis_registry.registry import register  # noqa: E402

RATING_MAP = {
    "买入": "Buy", "buy": "Buy", "Buy": "Buy",
    "观望": "Hold", "hold": "Hold", "Hold": "Hold",
    "卖出": "Sell", "sell": "Sell", "Sell": "Sell",
}


def record_dir() -> Path:
    import os
    env = os.environ.get("TRADINGAGENTS_SYSTEM_DIR")
    return Path(env) if env else Path.home() / ".tradingagents" / "system_analysis"


def register_one(ticker: str, trade_date: str, rating: str, paradigm: str = "",
                 score: float | None = None, confidence: str = "",
                 target: float | None = None, stop: float | None = None,
                 note: str = "", horizon: str = "") -> str | None:
    """写原文 JSON + 登记索引；返回 record_id（失败返回 None）。"""
    normalized = RATING_MAP.get(str(rating).strip())
    if not normalized:
        raise ValueError(f"rating 只支持 买入/观望/卖出（或 Buy/Hold/Sell），收到：{rating}")

    ts = int(time.time())
    d = record_dir()
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{ticker}_{trade_date}_{ts}.json"
    payload = {
        "ticker": ticker,
        "trade_date": trade_date,
        "parsed": {
            "direction": {"Buy": "买入", "Hold": "观望", "Sell": "卖出"}[normalized],
            "paradigm": paradigm,
            "score": score,
            "confidence": confidence,
            "horizon": horizon,
        },
        "levels": {k: v for k, v in (("target", target), ("stop", stop)) if v is not None},
        "note": note,
        "source": "投资分析体系",
        "registered_at": ts,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    return register(
        "system", ticker, trade_date, ts, path=str(path), name="",
        summary={
            "rating": normalized, "direction": payload["parsed"]["direction"],
            "paradigm": paradigm, "score": score, "confidence": confidence,
            "note": note[:200],
        },
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticker", required=True)
    ap.add_argument("--date", required=True, help="分析日 YYYY-MM-DD")
    ap.add_argument("--rating", required=True, help="买入/观望/卖出（或 Buy/Hold/Sell）")
    ap.add_argument("--paradigm", default="", help="主范式")
    ap.add_argument("--score", type=float, default=None)
    ap.add_argument("--confidence", default="")
    ap.add_argument("--target", type=float, default=None)
    ap.add_argument("--stop", type=float, default=None)
    ap.add_argument("--horizon", default="", help="3-5月 / 3-5年")
    ap.add_argument("--note", default="")
    args = ap.parse_args()

    rid = register_one(args.ticker, args.date, args.rating, args.paradigm,
                       args.score, args.confidence, args.target, args.stop,
                       args.note, args.horizon)
    if not rid:
        print("[fail] 登记失败（检查 registry 目录权限或参数）")
        return 1
    print(f"[ok] 已登记 {rid}")
    print(f"     10 个交易日后由 scripts/run_validations.py 自动验证（kind=system）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
