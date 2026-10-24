"""M1 数据仓建仓脚本：拉日线，写 Parquet + SQLite 元数据。

用法：
  # 小样本冒烟
  python scripts/build_warehouse.py --days 3 --symbols seed

  # 全市场（新浪快照给代码表/名称/当日成交额），默认 4 并发
  python scripts/build_warehouse.py --days 250 --universe --workers 4

说明：
- 全市场代码表与名称、**当日成交额**都取自新浪快照（东财 clist 在本机不可用）；
- 历史成交额仍为 null（新浪/腾讯日线不含），只有最新交易日能补上；
- 数据目录默认 ./data/warehouse，可用 TRADINGAGENTS_DATA_DIR 覆盖。
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tradingagents.backtest.sources import (  # noqa: E402
    SEED_SYMBOLS, board_of, fetch_daily, fetch_market_snapshot,
    fetch_names, fetch_universe,
)
from tradingagents.backtest.store import Warehouse  # noqa: E402


def parse_symbols(arg: str) -> list[str]:
    if arg == "seed":
        return list(SEED_SYMBOLS)
    path = Path(arg).expanduser()
    if path.exists():
        return [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines()
                if ln.strip() and not ln.startswith("#")]
    return [s.strip() for s in arg.split(",") if s.strip()]


def main() -> int:
    ap = argparse.ArgumentParser(description="M1 回测数据仓建仓")
    ap.add_argument("--days", type=int, default=3, help="保留最近 N 个交易日")
    ap.add_argument("--symbols", default="seed", help="seed | 逗号分隔代码 | 文件路径")
    ap.add_argument("--universe", action="store_true", help="全市场（新浪快照代码表）")
    ap.add_argument("--limit", type=int, default=6000, help="全市场上限")
    ap.add_argument("--workers", type=int, default=4, help="并发取数线程数")
    ap.add_argument("--datalen", type=int, default=0, help="每只取多少根日线（0=自适应当前 --days）")
    ap.add_argument("--chunk", type=int, default=400, help="每多少只写一次盘")
    ap.add_argument("--out", default=os.environ.get("TRADINGAGENTS_DATA_DIR",
                                                    "data/warehouse"))
    args = ap.parse_args()

    # 1) 代码表 + 名称 + 当日成交额
    snap = fetch_market_snapshot() if args.universe else pd.DataFrame()
    if args.universe:
        symbols = snap["symbol"].tolist()[: args.limit] if len(snap) else []
        if not symbols:
            symbols = fetch_universe(args.limit)
        if not symbols:
            print("[warn] 全市场代码表不可用，降级到种子池")
            symbols = list(SEED_SYMBOLS)
    else:
        symbols = parse_symbols(args.symbols)
    print(f"[info] 标的数 {len(symbols)}，目标保留最近 {args.days} 个交易日")

    name_map = dict(zip(snap["symbol"], snap["name"])) if len(snap) else {}
    amount_map = dict(zip(snap["symbol"], snap["amount"])) if len(snap) else {}
    volume_map = dict(zip(snap["symbol"], snap["volume"])) if len(snap) else {}

    # 2) 交易日历：用一只高流动性标的的最近 N 根日线当参照
    datalen = args.datalen or max(args.days, 60)
    ref = fetch_daily("600519", datalen=max(datalen, args.days))
    dates = sorted(ref["trade_date"].unique())[-args.days:]
    date_set = set(dates)
    latest = dates[-1]
    print(f"[info] 交易日窗口 {dates[0]} ~ {latest}（{len(dates)} 天），"
          f"参照标的 600519 取到 {len(ref)} 根")

    # 3) 分块并发取数并落盘
    wh = Warehouse(args.out)
    wh.ensure_schema()
    total_rows = total_pool = 0
    failures: list[str] = []
    t0 = time.time()

    def one(sym: str):
        try:
            return sym, fetch_daily(sym, datalen=max(datalen, len(dates))), None
        except Exception as e:
            return sym, None, f"{sym}: {e}"

    for start in range(0, len(symbols), args.chunk):
        batch = symbols[start:start + args.chunk]
        frames = []
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            for sym, df, err in pool.map(one, batch):
                if err:
                    failures.append(err)
                elif df is not None:
                    frames.append(df)
        if not frames:
            print(f"[warn] {start}-{start+len(batch)} 全部失败，跳过")
            continue

        chunk_df = pd.concat(frames, ignore_index=True)
        chunk_df = chunk_df[chunk_df["trade_date"].isin(date_set)].copy()
        if chunk_df.empty:
            continue
        # 最新交易日的成交额用快照回填（历史仍为 null）
        chunk_df["amount"] = chunk_df.apply(
            lambda r: amount_map.get(r["symbol"]) if r["trade_date"] == latest else None,
            axis=1,
        )
        total_rows += wh.write_kline(chunk_df)

        names = {s: name_map.get(s) for s in chunk_df["symbol"].unique()}
        missing = [s for s, n in names.items() if not n]
        if missing:
            names.update(fetch_names(missing))
        pool_df = pd.DataFrame({
            "trade_date": chunk_df["trade_date"],
            "symbol": chunk_df["symbol"],
            "name": chunk_df["symbol"].map(names),
            "board": chunk_df["symbol"].map(board_of),
            "is_st": chunk_df["symbol"].map(
                lambda s: int("ST" in (names.get(s) or ""))),
            "is_suspended": 0,
        }).drop_duplicates(["trade_date", "symbol"])
        total_pool += wh.upsert_pool(pool_df)

        done = min(start + args.chunk, len(symbols))
        el = time.time() - t0
        rate = done / el if el else 0
        eta = (len(symbols) - done) / rate if rate else 0
        print(f"[info] {done}/{len(symbols)} 已写 {total_rows} 行，"
              f"{rate:.1f} 只/秒，预计剩余 {eta/60:.1f} 分钟")

    st = wh.stats()
    print(f"[done] 日线 {total_rows} 行 / 股票池 {total_pool} 行；"
          f"日线 {st['kline_files']} 个 {st['kline_bytes']/1024/1024:.1f} MB，"
          f"股票池 {st['pool_files']} 个 {st['pool_bytes']/1024/1024:.1f} MB，"
          f"meta.db {st['db_bytes']/1024/1024:.1f} MB")
    print(f"[done] 用时 {(time.time()-t0)/60:.1f} 分钟；目录 {Path(args.out).resolve()}")
    if failures:
        fp = Path(args.out) / "failures.txt"
        fp.write_text("\n".join(failures), encoding="utf-8")
        print(f"[warn] 失败 {len(failures)} 只，清单见 {fp}")
        for f in failures[:5]:
            print("   ", f)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
