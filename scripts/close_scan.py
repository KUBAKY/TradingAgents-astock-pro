#!/usr/bin/env python3
"""盘后全市场选股扫描（launchd 定时任务入口）。

周一至五 15:10 由 ~/Library/LaunchAgents/com.tradingagents.close-scan.plist 触发。
- 从项目 .env 读取 provider/key（launchd 环境不继承 shell env）
- 自动选 provider：AUTOSCAN_PROVIDER > 有 DEEPSEEK_API_KEY 则 deepseek > ST_PROVIDER
- 结果落盘（--save-record）+ 输出到 stdout（plist 重定向到日志文件）

用法: python scripts/close_scan.py [额外 screener 参数]
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(_PROJECT_ROOT / ".env", override=True)


def _pick_provider() -> tuple[str, str]:
    """返回 (provider, model)。launchd 无终端 key，全部从 .env 解析。"""
    provider = os.environ.get("AUTOSCAN_PROVIDER", "")
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
    from tradingagents.shortterm import screener

    provider, model = _pick_provider()
    args = list(sys.argv[1:])
    if not any(a in ("--provider", "-p") for a in args):
        args = ["--provider", provider] + args
    if not any(a in ("--model", "-m") for a in args):
        args += ["--model", model]
    if "--save-record" not in args:
        args.append("--save-record")
    print(f"[close-scan] provider={provider} model={model} args={args}", flush=True)
    sys.argv = ["screener"] + args
    screener.main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
