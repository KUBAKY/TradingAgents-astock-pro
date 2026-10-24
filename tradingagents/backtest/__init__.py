"""回测数据仓（M1）。

设计见 docs/BACKTEST_DESIGN.md：
- 日线按年存 Parquet（列式压缩，分析型读取快）
- 元数据存 SQLite（股票池 / 财务 asof / 公司行为 / 主题归属）
- 所有"历史可见"查询必须带 asof，避免未来函数
"""

from tradingagents.backtest.store import Warehouse
from tradingagents.backtest.sources import (
    SEED_SYMBOLS,
    fetch_daily,
    fetch_names,
    fetch_universe,
)

__all__ = ["Warehouse", "SEED_SYMBOLS", "fetch_daily", "fetch_names", "fetch_universe"]
