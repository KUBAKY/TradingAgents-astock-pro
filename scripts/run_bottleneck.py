"""M3：产业链环节级回测（瓶颈模块试点）。

对每个主题：按环节等权买入持有，比较各环节区间收益，
检验「事前判定的瓶颈环节」是否跑赢「终端环节」，并给出离散度。

用法：
  python scripts/run_bottleneck.py            # 三个主题全跑
  python scripts/run_bottleneck.py --theme 锂电

注意：标的是事后挑选（见 baskets.py 声明），本脚本只做"环节间比较"，
不构成选股能力证据。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tradingagents.backtest.baskets import THEMES  # noqa: E402
from tradingagents.backtest.store import Warehouse  # noqa: E402


def segment_return(kline: pd.DataFrame, symbols: list[str], start: str, end: str):
    """环节等权：各标的 (期末/期初 - 1)，再取平均。缺失标的剔除并注明。"""
    rets, missing = [], []
    for s in symbols:
        d = kline[(kline.symbol == s) & (kline.trade_date >= start) & (kline.trade_date <= end)]
        d = d.sort_values("trade_date")
        if len(d) < 20:
            missing.append(s)
            continue
        rets.append(d["close"].iloc[-1] / d["close"].iloc[0] - 1)
    return (sum(rets) / len(rets) if rets else float("nan")), missing


def run_theme(name: str, spec: dict, wh: Warehouse) -> None:
    start, end = spec["window"]
    print(f"\n=== {name}（{start} ~ {end}）===")
    rows, missing_all = [], []
    for seg, symbols in spec["segments"].items():
        kline = wh.read_kline(symbols=symbols, start=start, end=end)
        ret, miss = segment_return(kline, symbols, start, end)
        missing_all += miss
        rows.append({"环节": seg, "区间收益": ret, "标的数": len(symbols) - len(miss)})

    df = pd.DataFrame(rows).sort_values("区间收益", ascending=False)
    best, worst = df.iloc[0], df.iloc[-1]
    bn = spec["bottleneck"]
    bn_ret = df.loc[df["环节"] == bn, "区间收益"].iloc[0]
    term = df.iloc[-1]["环节"]

    for _, r in df.iterrows():
        mark = " ← 事前判定瓶颈" if r["环节"] == bn else ""
        print(f"  {r['环节']:<10} {r['区间收益']*100:8.2f}%  标的 {int(r['标的数'])}{mark}")
    print(f"  最强环节: {best['环节']} ({best['区间收益']*100:.2f}%) | "
          f"最弱: {worst['环节']} ({worst['区间收益']*100:.2f}%) | "
          f"离散度 {(best['区间收益']-worst['区间收益'])*100:.2f}pp")
    print(f"  瓶颈（{bn}）{bn_ret*100:.2f}% vs 最弱环节 {worst['区间收益']*100:.2f}% → "
          f"{'跑赢' if bn_ret > worst['区间收益'] else '跑输'} "
          f"{(bn_ret-worst['区间收益'])*100:+.2f}pp")
    if missing_all:
        print(f"  [warn] 缺数据/样本不足的标的: {sorted(set(missing_all))}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--theme", help="主题名关键词过滤")
    ap.add_argument("--data-dir", default="data/warehouse")
    args = ap.parse_args()

    wh = Warehouse(args.data_dir)
    themes = {k: v for k, v in THEMES.items()
              if not args.theme or args.theme in k}
    if not themes:
        print("没有匹配的主题")
        return 1
    for name, spec in themes.items():
        run_theme(name, spec, wh)
    print("\n[说明] 标的为事后挑选，结论仅用于环节间比较，不构成选股能力证据。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
