"""生成 A 股黑名单（体系 `AS-C0-11`）。

只用**可数据核验**的硬条件，不做判断：
  ST / 次新 / 停牌 / 面值(<1.5) / 流动性(成交额<1000万)
尚无法自动判定的两项（资不抵债、审计非标/立案）在输出里标为「季度补充」，需财报或公告。

用法：
  python scripts/build_blacklist.py                      # 今日快照
  python scripts/build_blacklist.py --out ../../my-wiki/.../A股黑名单.md   # 同时写 markdown
  python scripts/build_blacklist.py --json data/blacklist.json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from datetime import date
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tradingagents.backtest.store import Warehouse  # noqa: E402

MIN_PRICE = 1.5           # 面值退市 1 元，留缓冲
MIN_AMOUNT = 1e7          # 1000 万元
NEW_STOCK_DAYS = 60
SUSPEND_DAYS = 30


def _announcement_reasons(sym: str, start: str) -> list[str]:
    """巨潮公告初筛：非标 / 立案 / 退市风险。"""
    from tradingagents.dataflows.cninfo import classify, fetch_announcements
    rows = fetch_announcements(sym, start, date.today().isoformat(), page_size=30)
    cats = classify([r["title"] for r in rows])
    out = []
    if "审计非标" in cats:
        out.append(f"审计非标({cats['审计非标'][0][:24]})")
    if "立案调查" in cats:
        out.append(f"立案调查({cats['立案调查'][0][:24]})")
    if "退市风险" in cats:
        out.append(f"退市风险({cats['退市风险'][0][:24]})")
    return out


def _net_assets_negative(sym: str) -> bool | None:
    """新浪资产负债表：负债合计 > 资产总计。取不到返回 None。"""
    import io
    import pandas as pd
    from tradingagents.dataflows.a_stock import _get_financial_report_sina
    try:
        df = _get_financial_report_sina(sym, "资产负债表", "quarterly")
        if df.empty:
            return None
        row = df.iloc[0]
        assets = float(row.get("资产总计"))
        liab = float(row.get("负债合计"))
        return liab > assets
    except Exception:
        return None


def build(wh: Warehouse, asof: str | None = None,
          announcement_limit: int = 0, net_asset_limit: int = 0) -> tuple[dict, str]:
    k = wh.read_kline()
    latest = asof or k["trade_date"].max()
    dates = sorted(k["trade_date"].unique())
    recent = dates[-NEW_STOCK_DAYS:]
    pool = wh.pool_asof(latest).set_index("symbol")

    reasons: dict[str, list[str]] = defaultdict(list)
    detail: dict[str, dict] = {}

    for sym, g in k[k["trade_date"].isin(recent)].groupby("symbol"):
        g = g.sort_values("trade_date")
        name = pool["name"].get(sym) if sym in pool.index else ""
        why: list[str] = []

        if name and "ST" in name:
            why.append("ST")
        if g["trade_date"].iloc[0] > recent[0] and len(g) < NEW_STOCK_DAYS:
            why.append("次新股(上市不足60交易日)")
        zero_days = int((g["volume"] <= 0).sum())
        if zero_days >= SUSPEND_DAYS:
            why.append(f"长期停牌(近60日{zero_days}日无成交)")
        elif zero_days > 0 and len(g) and g["trade_date"].iloc[-1] == latest and g["volume"].iloc[-1] <= 0:
            why.append("最新交易日停牌")

        last = g[g["trade_date"] == latest]
        if not last.empty:
            px = float(last["close"].iloc[0])
            if px < MIN_PRICE:
                why.append(f"面值风险({px:.2f}元<{MIN_PRICE})")
            amt = last["amount"].iloc[0]
            if pd.notna(amt) and float(amt) < MIN_AMOUNT:
                why.append(f"流动性枯竭(成交额{float(amt)/1e4:.0f}万)")

        if why:
            for r in why:
                reasons[r.split("(")[0]].append(sym)
            detail[sym] = {"name": name, "reasons": why,
                           "price": float(last["close"].iloc[0]) if not last.empty else None}

    # 公告初筛（仅对已命中 ST/面值 或 价格<3 元的候选，控制请求量）
    if announcement_limit:
        cands = [k2 for k2, v in detail.items()
                 if v.get("price") is not None and (v["price"] < 3 or any("ST" in r for r in v["reasons"]))][:announcement_limit]
        start = f"{int(latest[:4])-1}-01-01"
        for sym in cands:
            extra = _announcement_reasons(sym, start)
            if extra:
                detail[sym]["reasons"] += extra
                reasons["公告硬条件"].append(sym)
            time.sleep(0.3)

    # 资不抵债（仅对 ST/低价候选，逐只取资产负债表）
    if net_asset_limit:
        cands = [k2 for k2, v in detail.items()
                 if v.get("price") is not None and (v["price"] < 5 or any("ST" in r for r in v["reasons"]))][:net_asset_limit]
        for sym in cands:
            neg = _net_assets_negative(sym)
            if neg:
                detail[sym]["reasons"].append("资不抵债(负债>资产)")
                reasons["资不抵债"].append(sym)

    result = {
        "asof": latest,
        "generated_at": date.today().isoformat(),
        "criteria": {
            "ST": "简称含 ST",
            "次新股": f"上市不足 {NEW_STOCK_DAYS} 个交易日",
            "停牌": f"最新交易日无成交，或近 {NEW_STOCK_DAYS} 日停牌 ≥{SUSPEND_DAYS} 日",
            "面值风险": f"收盘价 < {MIN_PRICE} 元",
            "流动性枯竭": f"最新交易日成交额 < {MIN_AMOUNT/1e4:.0f} 万元",
            "未覆盖": ["资不抵债(净资产<0)", "审计非标/立案调查"],
        },
        "count": len(detail),
        "by_reason": {k2: len(v) for k2, v in reasons.items()},
        "symbols": detail,
    }
    return result, latest


def to_markdown(res: dict) -> str:
    lines = [f"# A股黑名单（{res['asof']}）", "",
             f"> 生成时间 {res['generated_at']}｜命中 **{res['count']}** 只｜"
             f"由 `scripts/build_blacklist.py` 生成，勿手改", "",
             "## 命中统计", "", "| 条件 | 只数 |", "|---|---|"]
    for k, v in sorted(res["by_reason"].items(), key=lambda x: -x[1]):
        lines.append(f"| {k} | {v} |")
    lines += ["", "## 名单（代码 · 名称 · 原因）", "",
              "| 代码 | 名称 | 原因 |", "|---|---|---|"]
    for sym, d in sorted(res["symbols"].items()):
        lines.append(f"| {sym} | {d['name']} | {'；'.join(d['reasons'])} |")
    lines += ["", "## 未覆盖（需季度补）", "",
              "- 资不抵债（净资产 < 0）：需批量财报",
              "- 非标审计意见 / 立案调查：需公告批量扫描", ""]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default="data/warehouse")
    ap.add_argument("--json", default="data/blacklist.json")
    ap.add_argument("--out", help="同时写 markdown 到指定路径")
    ap.add_argument("--asof", help="指定交易日（默认最新）")
    ap.add_argument("--with-announcements", type=int, default=0,
                    help="对前 N 只候选查巨潮公告（补非标/立案/退市风险）")
    ap.add_argument("--with-net-assets", type=int, default=0,
                    help="对前 N 只候选查净资产（补资不抵债）")
    args = ap.parse_args()

    wh = Warehouse(args.data_dir)
    res, latest = build(wh, args.asof,
                        announcement_limit=args.with_announcements,
                        net_asset_limit=args.with_net_assets)
    Path(args.json).parent.mkdir(parents=True, exist_ok=True)
    Path(args.json).write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"数据日 {latest}：黑名单 {res['count']} 只")
    for k, v in sorted(res["by_reason"].items(), key=lambda x: -x[1]):
        print(f"  {k}: {v}")
    print(f"JSON → {args.json}")

    if args.out:
        md = to_markdown(res)
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(md, encoding="utf-8")
        print(f"Markdown → {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
