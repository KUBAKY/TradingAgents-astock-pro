#!/usr/bin/env python3
"""集合竞价验证脚本 — 交易日 09:15-09:30 运行。

用法:
    python scripts/check_auction.py                # 默认观察池
    python scripts/check_auction.py 300750 600519  # 指定代码

输出: 每票竞价摘要 + 通过 _collect_auction 走 ch0 实时窗口路径。
"""
import argparse
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tradingagents.shortterm.ch0 import _collect_auction

DEFAULT_WATCH = ["300750", "600519", "000725"]


def main() -> int:
    ap = argparse.ArgumentParser(description="集合竞价实时验证")
    ap.add_argument("codes", nargs="*", help="股票代码，缺省用观察池")
    args = ap.parse_args()
    codes = args.codes or DEFAULT_WATCH

    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    print(f"[auction-check] {now:%Y-%m-%d %H:%M:%S} 窗口: 09:15-09:30")
    if not ("09:15" <= now.strftime("%H:%M") < "09:30"):
        print(f"[auction-check] 当前 {now:%H:%M} 不在竞价窗口，本脚本应于 09:15-09:30 运行")
        print("[auction-check] 直接调用 get_auction_data 验证数据通路:")
        from tradingagents.dataflows.a_stock import get_auction_data
        for c in codes:
            d = get_auction_data(c, today)
            print(f"  {c}: {d}")
        return 0

    ok = True
    for c in codes:
        d = _collect_auction(c, today, now=now)
        if d is False:
            print(f"[auction-check] {c}: 采集失败")
            ok = False
        elif d is None:
            print(f"[auction-check] {c}: 窗口判定未命中（跳过）")
        else:
            print(
                f"[auction-check] {c}: 开盘价 {d['open_px']} ({d['open_pct']:+.2f}%) "
                f"→ 匹配价 {d['final_px']} ({d['final_pct']:+.2f}%) "
                f"区间 [{d['low_px']},{d['high_px']}] "
                f"累计量 {d['vol']} 手 匹配量 {d['final_vol']} 手 笔数 {d['ticks']}"
            )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
