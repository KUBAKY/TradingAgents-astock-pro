"""事件驱动回测引擎（M2）。

时序约定（与 docs/BACKTEST_DESIGN.md 一致）：
- T 日收盘后由策略给出目标权重 → 生成订单；
- 订单在 **T+1 开盘** 撮合（不用当日收盘价，避免"收盘后才可知"的幻觉）；
- T+1 买入的股份当日不可卖（A 股 T+1），次日解冻；
- 一字涨停不成交（买入）、一字跌停不成交（卖出）、停牌不成交；
- 费用：卖出印花税 0.05%、佣金双边万 2.5（最低 5 元）、过户费 0.001% 双边、滑点按成交额 0.1%。

本模块只做撮合与记账，不判断策略优劣。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from tradingagents.backtest.risk import apply_risk

# ---------- 交易规则 ----------


def limit_pct(symbol: str, is_st: bool = False) -> float:
    """涨跌停幅度：ST 5%，科创/创业 20%，北交所 30%，其余 10%。"""
    if is_st:
        return 0.05
    s = str(symbol)
    if s.startswith(("688", "689", "300", "301")):
        return 0.20
    if s.startswith(("4", "8", "92")):
        return 0.30
    return 0.10


@dataclass(frozen=True)
class CostConfig:
    stamp_duty: float = 0.0005      # 卖出单边
    commission: float = 0.00025     # 双边
    min_commission: float = 5.0
    transfer_fee: float = 0.00001   # 双边
    slippage: float = 0.001         # 单边

    def buy_cost(self, amount: float) -> float:
        return max(amount * self.commission, self.min_commission) + amount * self.transfer_fee

    def sell_cost(self, amount: float) -> float:
        return (amount * self.stamp_duty
                + max(amount * self.commission, self.min_commission)
                + amount * self.transfer_fee)


@dataclass
class Position:
    shares: int = 0
    available: int = 0        # T+1：当日可卖
    cost: float = 0.0         # 累计买入成本（含费用）
    last_price: float = 0.0

    @property
    def market_value(self) -> float:
        return self.shares * self.last_price


@dataclass
class Fill:
    trade_date: str
    symbol: str
    side: str          # buy / sell
    shares: int
    price: float
    amount: float
    fee: float


@dataclass
class BacktestResult:
    equity: pd.DataFrame          # date, equity, cash, position_value, drawdown
    fills: list[Fill]
    rejected: list[dict] = field(default_factory=list)
    risk_notes: list[str] = field(default_factory=list)


class BacktestEngine:
    """单账户、日频、次日开盘撮合的引擎。"""

    def __init__(self, data: pd.DataFrame, cash: float = 1_000_000.0,
                 cost: CostConfig | None = None, max_weight: float = 1.0,
                 lot: int = 100, risk=None):
        """risk: risk.RiskConfig；给了就启用 7.5/7.6 的组合风控（策略需返回 Signal 列表）。"""
        if data.empty:
            raise ValueError("回测数据为空")
        self.cost = cost or CostConfig()
        self.max_weight = max_weight
        self.lot = lot
        self.risk = risk
        self.risk_notes: list[str] = []
        self.cash = float(cash)
        self.positions: dict[str, Position] = {}
        self.fills: list[Fill] = []
        self.rejected: list[dict] = []

        df = data.copy()
        df["trade_date"] = df["trade_date"].astype(str)
        df = df.sort_values(["symbol", "trade_date"])
        # pre_close 用于涨跌停判定；仓里没存就按上一交易日收盘推
        if "pre_close" not in df.columns or df["pre_close"].isna().all():
            df["pre_close"] = df.groupby("symbol")["close"].shift(1)
        self.data = df
        self.by_date = {d: g.set_index("symbol") for d, g in df.groupby("trade_date")}
        self.calendar = sorted(self.by_date)
        self._pending: dict[str, float] | None = None   # 上一日收盘生成的目标权重

    # ---------- 撮合 ----------
    def _fill_price(self, row, side: str) -> float:
        px = float(row["open"])
        return px * (1 + self.cost.slippage) if side == "buy" else px * (1 - self.cost.slippage)

    def _tradeable(self, symbol: str, row, side: str, is_st: bool) -> tuple[bool, str]:
        if pd.isna(row.get("open")) or float(row["open"]) <= 0:
            return False, "无有效开盘价"
        if float(row.get("volume") or 0) <= 0:
            return False, "停牌/无成交"
        pre = row.get("pre_close")
        if pd.isna(pre) or float(pre) <= 0:
            return True, ""
        pct = limit_pct(symbol, is_st)
        up, down = float(pre) * (1 + pct), float(pre) * (1 - pct)
        o, h, l = float(row["open"]), float(row["high"]), float(row["low"])
        if side == "buy" and abs(o - up) < 1e-6 and abs(h - l) < 1e-6:
            return False, "一字涨停买不到"
        if side == "sell" and abs(o - down) < 1e-6 and abs(h - l) < 1e-6:
            return False, "一字跌停卖不掉"
        return True, ""

    def _execute(self, pending, date: str) -> None:
        bar = self.by_date[date]
        equity = self.cash + sum(p.market_value for p in self.positions.values())
        weights = self._resolve_weights(pending, equity)
        targets: dict[str, float] = {}
        for sym, w in weights.items():
            w = max(0.0, min(float(w), self.max_weight))
            if w > 0 and sym in bar.index:
                targets[sym] = w

        # 先卖后买（释放现金）
        for sym, pos in list(self.positions.items()):
            target_value = equity * targets.get(sym, 0.0)
            if pos.available <= 0 or pos.market_value <= target_value * 1.0001:
                continue
            if sym not in bar.index:
                continue
            row = bar.loc[sym]
            ok, why = self._tradeable(sym, row, "sell", False)
            if not ok:
                self.rejected.append({"date": date, "symbol": sym, "side": "sell", "reason": why})
                continue
            px = self._fill_price(row, "sell")
            sell_shares = min(pos.available, int((pos.market_value - target_value) / px))
            sell_shares -= sell_shares % self.lot
            if sell_shares <= 0:
                continue
            amount = sell_shares * px
            fee = self.cost.sell_cost(amount)
            self.cash += amount - fee
            pos.shares -= sell_shares
            pos.available -= sell_shares
            pos.cost -= pos.cost * sell_shares / max(1, sell_shares + pos.shares) if pos.shares + sell_shares else 0
            self.fills.append(Fill(date, sym, "sell", sell_shares, px, amount, fee))
            if pos.shares == 0:
                self.positions.pop(sym, None)

        # 再买
        for sym, w in targets.items():
            row = bar.loc[sym]
            pos = self.positions.get(sym) or Position()
            target_value = equity * w
            gap = target_value - pos.market_value
            if gap <= 0:
                continue
            ok, why = self._tradeable(sym, row, "buy", False)
            if not ok:
                self.rejected.append({"date": date, "symbol": sym, "side": "buy", "reason": why})
                continue
            px = self._fill_price(row, "buy")
            budget = min(gap, self.cash)
            shares = int(budget / px / self.lot) * self.lot
            if shares <= 0:
                continue
            amount = shares * px
            fee = self.cost.buy_cost(amount)
            if amount + fee > self.cash:
                shares -= self.lot
                if shares <= 0:
                    continue
                amount = shares * px
                fee = self.cost.buy_cost(amount)
            self.cash -= amount + fee
            pos.shares += shares
            pos.cost += amount + fee
            self.positions[sym] = pos
            self.fills.append(Fill(date, sym, "buy", shares, px, amount, fee))

    # ---------- 主循环 ----------
    def _resolve_weights(self, pending, equity: float) -> dict[str, float]:
        """策略可直接给 {symbol: weight}，也可给 [Signal]（后者走 7.5/7.6 风控）。"""
        if isinstance(pending, dict):
            return pending
        if self.risk is None:
            return {s.symbol: s.weight for s in pending}
        weights, notes = apply_risk(pending, equity, self.risk)
        self.risk_notes.extend(notes)
        return weights

    def run(self, strategy) -> BacktestResult:
        records = []
        for i, date in enumerate(self.calendar):
            # 1) 开盘撮合昨日信号（空 dict 表示"清仓"，是有效信号，不能当没信号）
            if self._pending is not None:
                self._execute(self._pending, date)
                self._pending = None
            # 2) 收盘估值 + T+1 解冻
            bar = self.by_date[date]
            for sym, pos in self.positions.items():
                if sym in bar.index:
                    pos.last_price = float(bar.loc[sym, "close"])
                pos.available = pos.shares
            equity = self.cash + sum(p.market_value for p in self.positions.values())
            records.append({"trade_date": date, "equity": equity, "cash": self.cash,
                            "position_value": equity - self.cash, "n_positions": len(self.positions)})
            # 3) 生成明日目标权重
            history = self.data[self.data["trade_date"] <= date]
            self._pending = strategy.on_bar(date, history, self.positions, equity)

        eq = pd.DataFrame(records)
        eq["peak"] = eq["equity"].cummax()
        eq["drawdown"] = eq["equity"] / eq["peak"] - 1
        return BacktestResult(equity=eq, fills=self.fills, rejected=self.rejected)
