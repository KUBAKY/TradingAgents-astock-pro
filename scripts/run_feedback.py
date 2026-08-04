#!/usr/bin/env python3
"""生成验证误差分析报告（plan 设计 Step 5）。

读取注册表索引 validation 字段（零重算），聚合错判率 → 写
~/.tradingagents/analysis_registry/reports/feedback_<asof>.md，打印摘要。

用法: python scripts/run_feedback.py [--asof YYYY-MM-DD]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from tradingagents.analysis_registry.feedback import build_feedback_report  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="验证误差分析报告")
    ap.add_argument("--asof", default=None, help="报告日期 YYYY-MM-DD（默认今天）")
    args = ap.parse_args()

    rep = build_feedback_report(asof=args.asof)
    print(f"已评分 {rep['total']} ｜ 对 {rep['wins']} ｜ 错 {rep['losses']}"
          f" ｜ 胜率 {rep['win_rate']}%")
    print(f"报告: {rep['path']}")
    print("-" * 60)
    print(rep["markdown"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
