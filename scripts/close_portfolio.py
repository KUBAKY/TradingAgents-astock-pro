#!/usr/bin/env python3
"""盘后持仓自动跟进（launchd 定时任务入口）。

周一至五 15:15 由 ~/Library/LaunchAgents/com.tradingagents.close-portfolio.plist 触发。
- 从项目 .env 读取 provider/key（launchd 环境不继承 shell env）
- 对全部持仓跑每日跟进（割/持/补决策卡），幂等：当日已跟进则跳过
- 输出每票方向 + 总盈亏到 stdout（plist 重定向到日志文件）

用法: python scripts/close_portfolio.py [--date YYYY-MM-DD] [--force]
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(_PROJECT_ROOT / ".env", override=True)


def _pick_provider() -> tuple[str, str]:
    provider = os.environ.get("PORTFOLIO_PROVIDER", "")
    if not provider:
        if os.environ.get("DEEPSEEK_API_KEY"):
            provider = "deepseek"
        else:
            provider = os.environ.get("ST_PROVIDER", "anthropic")
    models = {
        "anthropic": "claude-haiku-4-5",
        "deepseek": "deepseek-v4-flash",
        "minimax": "minimax-text-01",
        "qwen": "qwen-max",
        "glm": "glm-4-plus",
    }
    return provider, models.get(provider, "deepseek-v4-flash")


def main() -> int:
    from tradingagents.shortterm import portfolio

    p = argparse.ArgumentParser(description="盘后持仓跟进")
    p.add_argument("--date", default=None, help="交易日期（默认今天）")
    p.add_argument("--force", action="store_true", help="当日已跟进也强制重跑")
    args = p.parse_args()

    provider, model = _pick_provider()
    trade_date = args.date or portfolio._date.today().isoformat()

    positions = portfolio.list_positions()
    if not positions:
        print("[close-portfolio] 无持仓，跳过", flush=True)
        return 0

    print(f"[close-portfolio] date={trade_date} provider={provider} model={model} "
          f"positions={len(positions)} force={args.force}", flush=True)
    result = portfolio.run_daily_follow(
        trade_date, provider=provider, model=model, force=args.force)

    if result.get("skipped"):
        print("[close-portfolio] 当日已跟进，跳过（--force 可重跑）", flush=True)
        return 0

    for r in result["results"]:
        if r.get("error"):
            print(f"  ✗ {r['ticker']} {r.get('name', '')}: {r['error']}", flush=True)
        else:
            print(f"  ✓ {r['ticker']} {r.get('name', '')}: "
                  f"方向={r.get('direction') or '未解析'} "
                  f"置信={r.get('confidence') or '-'} "
                  f"成本¥{r['cost_cny'] or 0:.4f}", flush=True)
    snap = result.get("snapshot") or {}
    print(f"[close-portfolio] 持仓市值 ¥{snap.get('total_value', 0):,.2f} "
          f"浮动盈亏 ¥{snap.get('total_pnl', 0):,.2f} "
          f"失败 {result.get('failed', 0)}", flush=True)
    return 1 if result.get("failed") else 0


if __name__ == "__main__":
    raise SystemExit(main())
