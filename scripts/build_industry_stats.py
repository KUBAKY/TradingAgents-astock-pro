"""行业中位数聚合 CLI。

用法：
  python scripts/build_industry_stats.py --map                    # 建行业归属表（快，约 1 分钟）
  python scripts/build_industry_stats.py --symbol 300308 --year 2026 --quarter 2
  python scripts/build_industry_stats.py --symbol 300308 --year 2026 --quarter 2 --limit 20   # 快速验证

说明：先建 --map；单行业约 200–500 只，首次计算 10–20 分钟，之后走缓存。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tradingagents.dataflows.industry import (  # noqa: E402
    aggregate, build_industry_map, build_industry_map_baostock,
    collect_peer_metrics, fetch_sina_industries,
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--map", action="store_true", help="只建行业归属表")
    ap.add_argument("--symbol", help="按股票代码查其行业中位数")
    ap.add_argument("--year", type=int, default=2026)
    ap.add_argument("--quarter", type=int, default=2)
    ap.add_argument("--limit", type=int, default=0, help="只算前 N 只（验证用）")
    ap.add_argument("--no-balance", action="store_true", help="跳过资产负债表（省 1/3 时间，但无负债率）")
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--build-map-baostock", action="store_true",
                    help="用证监会行业建全量映射（准确，约 110 分钟，后台跑）")
    ap.add_argument("--peers", help="手工指定同行代码，逗号分隔（跳过行业映射）")
    args = ap.parse_args()

    data_dir = Path(args.data_dir)

    if args.build_map_baostock:
        m = build_industry_map_baostock(
            data_dir / "industry_map_baostock.json",
            progress=lambda i, n: print(f"[map] {i}/{n}", flush=True))
        print(f"[done] 证监会行业映射 {len(m)} 只 → {data_dir/'industry_map_baostock.json'}")
        return 0

    if args.map or not args.symbol:
        inds = fetch_sina_industries()
        print(f"[info] 新浪行业 {len(inds)} 个，开始拉成员…")
        mapping = build_industry_map(data_dir / "industry_map.json")
        print(f"[done] 行业归属 {len(mapping)} 只 → {data_dir/'industry_map.json'}")
        if args.map:
            return 0

    if args.peers:
        peers = [x.strip() for x in args.peers.split(",") if x.strip()]
        peers = [x for x in peers if x != str(args.symbol)]   # 剔除自身，避免污染中位数
        industry = "手工名单"
    else:
        mapping = build_industry_map(data_dir / "industry_map.json")
        industry = mapping.get(str(args.symbol))
        if not industry:
            print(f"[fail] 未找到 {args.symbol} 的行业归属")
            return 1
        peers = [s for s, ind in mapping.items() if ind == industry]
    print(f"[info] {args.symbol} 属于「{industry}」，同行 {len(peers)} 只")

    period = f"{args.year}Q{args.quarter}"
    cache = data_dir / "industry_stats" / period / f"{industry}.json"
    if cache.exists() and not args.limit:
        result = json.loads(cache.read_text(encoding="utf-8"))
        print(f"[cache] 命中 {cache}")
    else:
        import baostock as bs
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()):
            bs.login()
        try:
            rows = collect_peer_metrics(
                peers, args.year, args.quarter,
                with_balance=not args.no_balance,
                limit=args.limit or None,
                progress=lambda i, n: print(f"[info] {i}/{n} 已取"),
            )
        finally:
            with contextlib.redirect_stdout(io.StringIO()):
                bs.logout()
        result = aggregate(rows, period, industry)
        if not args.limit:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"[done] 已缓存 {cache}")

    print(f"\n{industry} · {period} · 样本 {result['n']} 只")
    if result.get("warning"):
        print("⚠️", result["warning"])
    label = {"roeAvg": "ROE", "gpMargin": "毛利率", "npMargin": "净利率",
             "YOYNI": "净利同比", "liabilityToAsset": "资产负债率"}
    for k, v in result["metrics"].items():
        print(f"  {label.get(k,k):<8} 中位 {v['median']:>8.3f}  (P25 {v['p25']:.3f} / P75 {v['p75']:.3f}, n={v['n']})")

    # 目标股自身 vs 中位数（维度14 判定用）
    if args.symbol:
        import baostock as bs
        import contextlib, io
        with contextlib.redirect_stdout(io.StringIO()):
            bs.login()
        try:
            me = collect_peer_metrics([str(args.symbol)], args.year, args.quarter,
                                      with_balance=not args.no_balance)
        finally:
            with contextlib.redirect_stdout(io.StringIO()):
                bs.logout()
        if me:
            m = me[0]
            print(f"\n{args.symbol} 自身 vs 中位：")
            for k in ("roeAvg", "gpMargin", "npMargin", "YOYNI", "liabilityToAsset"):
                med = result["metrics"].get(k, {}).get("median")
                val = m.get(k)
                if val is None or med is None:
                    continue
                rel = f"{val/med:.2f}x" if med else "—"
                flag = "↑" if val > med else "↓"
                print(f"  {label.get(k,k):<8} {val:>8.3f}  vs 中位 {med:>8.3f}  {flag} {rel}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
