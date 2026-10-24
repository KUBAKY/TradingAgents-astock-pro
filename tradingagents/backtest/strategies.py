"""回测策略：只放最小可用的几种，用于校准引擎与跑基准。

策略接口：`on_bar(date, history, positions, equity) -> dict[symbol, target_weight]`
- `history` 只包含 <= date 的数据（引擎已切片），策略内部不要再取未来数据；
- 返回的权重在 **下一个交易日开盘** 撮合。
"""

from __future__ import annotations

import pandas as pd


class BuyAndHold:
    """等权买入并持有；不做再平衡。"""

    def __init__(self, symbols: list[str], weight_each: float | None = None):
        self.symbols = list(symbols)
        self.weight_each = weight_each

    def on_bar(self, date, history, positions, equity):
        if positions:                      # 已建仓就不再动
            return {s: p.market_value / equity for s, p in positions.items()}
        w = self.weight_each if self.weight_each is not None else 1.0 / len(self.symbols)
        return {s: w for s in self.symbols}


class WeightWithStops:
    """带止损与行业/主题标签的等权策略，用于验证 7.5/7.6 风控链路。

    specs: {symbol: {"stop": 0.08, "industry": "半导体", "theme": "AI算力",
                     "soft_veto": False, "unstable": False}}
    """

    def __init__(self, specs: dict[str, dict], weight_each: float = 1.0):
        self.specs = specs
        self.weight_each = weight_each
        self._entered = False

    def on_bar(self, date, history, positions, equity):
        if self._entered:
            return []
        self._entered = True
        from tradingagents.backtest.risk import Signal
        return [
            Signal(symbol=s, weight=self.weight_each,
                   stop=v.get("stop"), industry=v.get("industry", ""),
                   theme=v.get("theme", ""),
                   soft_veto=v.get("soft_veto", False),
                   unstable=v.get("unstable", False))
            for s, v in self.specs.items()
        ]


class PeriodicRebalance:
    """每 N 个交易日等权再平衡一次，用于检验撮合与费用的稳定性。"""

    def __init__(self, symbols: list[str], every: int = 20):
        self.symbols = list(symbols)
        self.every = every
        self._count = 0

    def on_bar(self, date, history, positions, equity):
        self._count += 1
        if self._count % self.every != 0:
            return {s: p.market_value / equity for s, p in positions.items()}
        w = 1.0 / len(self.symbols)
        return {s: w for s in self.symbols}
