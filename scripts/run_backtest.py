"""M2 回测 CLI。

用法：
  # 校准引擎（零成本 vs 解析解，误差需 <0.1%）
  python scripts/run_backtest.py --calibrate

  # 用数据仓里的标的跑买入持有
  python scripts/run_backtest.py --symbols 600519,000001 --start 2024-01-01 --end 2024-12-31

  # 直接取 ETF 数据（不在数据仓里）跑
  python scripts/run_backtest.py --symbols 512480 --start 2020-07-01 --end 2021-12-31 --fetch
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tradingagents.backtest.engine import BacktestEngine, CostConfig  # noqa: E402
from tradingagents.backtest.metrics import compute, format_metrics  # noqa: E402
from tradingagents.backtest.sources import fetch_daily  # noqa: E402
from tradingagents.backtest.store import Warehouse  # noqa: E402
from tradingagents.backtest.strategies import BuyAndHold  # noqa: E402

CALIBRATION_CASES = [
    ("512480 半导体ETF", "512480", "2020-07-01", "2021-12-31"),
    ("515880 通信ETF", "515880", "2023-01-01", "2024-06-30"),
    ("512480 半导体ETF", "512480", "2021-01-01", "2022-12-31"),
]


def load_data(symbols, start, end, data_dir, fetch: bool):
    if fetch:
        frames = [fetch_daily(s, datalen=1600) for s in symbols]
        df = pd.concat(frames, ignore_index=True)
    else:
        df = Warehouse(data_dir).read_kline(symbols=symbols, start=start, end=end)
    df = df[(df.trade_date >= start) & (df.trade_date <= end)]
    if df.empty:
        raise SystemExit(f"区间内无数据：{symbols} {start}~{end}")
    return df.sort_values(["symbol", "trade_date"])


def analytic_buy_hold(df: pd.DataFrame, symbol: str, cash: float, cost: CostConfig):
    """解析解：T 日信号 → T+1 开盘买入，持有到最后一日收盘。"""
    d = df[df.symbol == symbol].sort_values("trade_date")
    open_px = float(d.iloc[1]["open"]) * (1 + cost.slippage)
    last_close = float(d.iloc[-1]["close"])
    lot = 100
    shares = int(cash / open_px / lot) * lot
    amount = shares * open_px
    fee = cost.buy_cost(amount)
    while shares > 0 and amount + fee > cash:
        shares -= lot
        amount = shares * open_px
        fee = cost.buy_cost(amount)
    residual = cash - amount - fee
    return (shares * last_close + residual) / cash - 1


def run_case(label, symbol, start, end, cash, data_dir, fetch, calibrate=False):
    df = load_data([symbol], start, end, data_dir, fetch)
    zero = CostConfig(stamp_duty=0, commission=0, min_commission=0,
                      transfer_fee=0, slippage=0)
    engine = BacktestEngine(df, cash=cash, cost=zero if calibrate else CostConfig())
    res = engine.run(BuyAndHold([symbol]))
    m = compute(res)
    return df, res, m


def do_calibrate(args) -> int:
    print("== 引擎校准：零成本撮合 vs 解析解 ==")
    worst = 0.0
    for label, sym, start, end in CALIBRATION_CASES:
        try:
            df, res, m = run_case(label, sym, start, end, args.cash,
                                  args.data_dir, fetch=True, calibrate=True)
        except Exception as e:
            print(f"[FAIL] {label} {start}~{end}: {e}")
            worst = 1.0
            continue
        expected = analytic_buy_hold(df, sym, args.cash, CostConfig(
            stamp_duty=0, commission=0, min_commission=0, transfer_fee=0, slippage=0))
        actual = m["total_return"]
        err = abs(actual - expected)
        worst = max(worst, err)
        flag = "OK " if err < 0.001 else "BAD"
        print(f"[{flag}] {label:<16} {start}~{end}  "
              f"引擎 {actual*100:8.3f}%  解析 {expected*100:8.3f}%  "
              f"误差 {err*100:.4f}%  成交 {m['trades']} 笔")
    print(f"\n最大误差 {worst*100:.4f}%（阈值 0.1%）")
    return 0 if worst < 0.001 else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="M2 回测 CLI")
    ap.add_argument("--symbols", help="逗号分隔代码")
    ap.add_argument("--start", default="2020-01-01")
    ap.add_argument("--end", default="2026-10-23")
    ap.add_argument("--cash", type=float, default=1_000_000)
    ap.add_argument("--data-dir", default="data/warehouse")
    ap.add_argument("--fetch", action="store_true", help="直接联网取数（ETF 等不在仓里的标的）")
    ap.add_argument("--calibrate", action="store_true", help="跑引擎校准用例")
    args = ap.parse_args()

    if args.calibrate:
        return do_calibrate(args)
    if not args.symbols:
        ap.error("需要 --symbols 或 --calibrate")

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    df = load_data(symbols, args.start, args.end, args.data_dir, args.fetch)
    engine = BacktestEngine(df, cash=args.cash)
    res = engine.run(BuyAndHold(symbols))
    m = compute(res)

    print(f"标的 {symbols}  区间 {args.start}~{args.end}")
    print(format_metrics(m))
    if res.rejected:
        print(f"\n未成交 {len(res.rejected)} 次，前 5 条：")
        for r in res.rejected[:5]:
            print("  ", r)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
