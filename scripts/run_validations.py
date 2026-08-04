#!/usr/bin/env python3
"""盘后统一验证（launchd 定时任务入口，plan 设计 Step 4）。

周一至五 15:35 由 ~/Library/LaunchAgents/com.tradingagents.validate.plist 触发。
- 对注册表内全部分析记录（短线/扫描/深度复核/主线）做事后评估
- 幂等：同 asof 已有定论则跳过；重复运行安全
- 验证结果落盘 <registry>/validate/ 并回写索引 validation 字段
- 输出表格到 stdout（plist 重定向到日志文件）

用法: python scripts/run_validations.py [--ticker 000725] [--kind stock]
      [--asof YYYY-MM-DD] [--force] [--json]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from tradingagents.analysis_registry.validate import (  # noqa: E402
    run_validations,
)


def main() -> int:
    ap = argparse.ArgumentParser(description="统一验证：注册表记录事后评估")
    ap.add_argument("--ticker", default=None, help="只验证该票")
    ap.add_argument("--kind", default=None, choices=[
        "stock", "follow", "screen", "pick", "deep_review", "mainline"],
        help="只验证该方式")
    ap.add_argument("--asof", default=None, help="评估截止日 YYYY-MM-DD（默认今天）")
    ap.add_argument("--force", action="store_true", help="忽略幂等，强制重算")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()

    results = run_validations(
        ticker=args.ticker, kind=args.kind, asof=args.asof, force=args.force,
    )

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=1))
        return 0

    if not results:
        print("无记录可验证")
        return 0

    header = (f"{'verdict':<8} {'kind':<12} {'ticker':<8} {'trade_date':<12}"
              f" {'t3%':>7} {'t10%':>7} {'bars':>5} {'hit'}")
    print(header)
    print("-" * len(header))
    for r in results:
        t3 = r.get("t3_close_pct")
        t10 = r.get("t10_close_pct")
        fmt = lambda v: (f"{v:+.2f}" if isinstance(v, (int, float)) else "-")
        print(f"{str(r.get('verdict')):<8} {r.get('kind',''):<12} "
              f"{str(r.get('ticker','')):<8} {str(r.get('trade_date','')):<12} "
              f"{fmt(t3):>7} {fmt(t10):>7} {str(r.get('bars_after',0)):>5} "
              f"{str(r.get('hit_first') or '-'):>4}")
        for p in r.get("picks") or []:
            print(f"    └ {p.get('ticker')}: {p.get('verdict')} "
                  f"t3={fmt(p.get('t3_close_pct'))} "
                  f"t10={fmt(p.get('t10_close_pct'))} "
                  f"hit={p.get('hit_first') or '-'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
