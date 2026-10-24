"""回测绩效指标（对齐体系 Ch8 口径）。"""

from __future__ import annotations

import math

import pandas as pd

TRADING_DAYS = 252


def _round_trips(fills) -> list[dict]:
    """按标的用移动平均成本法还原每笔卖出对应的盈亏。"""
    book: dict[str, dict] = {}
    out = []
    for f in fills:
        st = book.setdefault(f.symbol, {"shares": 0, "cost": 0.0})
        if f.side == "buy":
            st["shares"] += f.shares
            st["cost"] += f.amount + f.fee
        else:
            if st["shares"] <= 0:
                continue
            avg = st["cost"] / st["shares"]
            pnl = (f.amount - f.fee) - avg * f.shares
            out.append({"symbol": f.symbol, "pnl": pnl, "date": f.trade_date})
            st["cost"] -= avg * f.shares
            st["shares"] -= f.shares
    return out


def compute(result) -> dict:
    eq = result.equity
    if len(eq) < 2:
        return {"error": "样本不足"}
    r = eq["equity"].pct_change().dropna()
    total = eq["equity"].iloc[-1] / eq["equity"].iloc[0] - 1
    years = len(eq) / TRADING_DAYS
    annual = (1 + total) ** (1 / years) - 1 if years > 0 and 1 + total > 0 else float("nan")
    vol = r.std() * math.sqrt(TRADING_DAYS)
    sharpe = (r.mean() * TRADING_DAYS) / vol if vol > 0 else float("nan")
    mdd = eq["drawdown"].min()

    trips = _round_trips(result.fills)
    wins = [t for t in trips if t["pnl"] > 0]
    losses = [t for t in trips if t["pnl"] <= 0]
    win_rate = len(wins) / len(trips) if trips else float("nan")
    gross_win = sum(t["pnl"] for t in wins)
    gross_loss = -sum(t["pnl"] for t in losses)
    profit_factor = gross_win / gross_loss if gross_loss > 0 else float("inf")

    turnover = (sum(f.amount for f in result.fills) / 2) / eq["equity"].mean()

    return {
        "total_return": total,
        "annual_return": annual,
        "volatility": vol,
        "sharpe": sharpe,
        "max_drawdown": mdd,
        "calmar": annual / abs(mdd) if mdd else float("nan"),
        "trades": len(result.fills),
        "round_trips": len(trips),
        "win_rate": win_rate,
        "profit_factor": profit_factor,
        "turnover": turnover,
        "final_equity": float(eq["equity"].iloc[-1]),
        "days": len(eq),
    }


def format_metrics(m: dict) -> str:
    def pct(x):
        return "—" if x != x else f"{x*100:.2f}%"

    def num(x):
        return "—" if x != x else f"{x:.2f}"

    return "\n".join([
        f"区间收益      {pct(m['total_return'])}",
        f"年化收益      {pct(m['annual_return'])}",
        f"年化波动      {pct(m['volatility'])}",
        f"夏普          {num(m['sharpe'])}",
        f"最大回撤      {pct(m['max_drawdown'])}",
        f"Calmar        {num(m['calmar'])}",
        f"成交笔数      {m['trades']}（平仓 {m['round_trips']} 次）",
        f"胜率          {pct(m['win_rate'])}",
        f"盈亏比        {num(m['profit_factor'])}",
        f"换手率(均)    {num(m['turnover'])}x",
        f"期末权益      {m['final_equity']:,.0f}",
        f"交易日        {m['days']}",
    ])
