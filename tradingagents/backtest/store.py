"""数据仓存储层：Parquet（日线）+ SQLite（元数据）。

时点原则（PIT）：
- 日线按 `trade_date` 过滤即可，回测循环只传 <= T 的区间；
- 财务必须按 `publish_date <= asof` 过滤，禁止用 `report_period`；
- 股票池用每日快照，禁止用当前代码表回溯。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd

KLINE_COLUMNS = [
    "symbol", "trade_date", "open", "high", "low", "close", "pre_close",
    "volume", "amount", "adj_factor", "is_suspended", "source",
]

_META_SCHEMA = """
CREATE TABLE IF NOT EXISTS stock_pool_daily (
  trade_date TEXT NOT NULL, symbol TEXT NOT NULL, name TEXT,
  board TEXT, is_st INTEGER DEFAULT 0, is_suspended INTEGER DEFAULT 0,
  list_date TEXT, delist_date TEXT, total_mv REAL, float_mv REAL,
  PRIMARY KEY (trade_date, symbol)
);
CREATE TABLE IF NOT EXISTS financial_asof (
  symbol TEXT NOT NULL, report_period TEXT NOT NULL,
  publish_date TEXT NOT NULL, update_time TEXT,
  item TEXT NOT NULL, value REAL,
  PRIMARY KEY (symbol, report_period, item)
);
CREATE INDEX IF NOT EXISTS idx_fin_asof ON financial_asof(symbol, publish_date);
CREATE TABLE IF NOT EXISTS corporate_action (
  symbol TEXT, ex_date TEXT, type TEXT, ratio REAL, cash REAL,
  PRIMARY KEY (symbol, ex_date, type)
);
CREATE TABLE IF NOT EXISTS theme_membership (
  theme TEXT, symbol TEXT, bk_code TEXT, asof TEXT NOT NULL, source TEXT,
  PRIMARY KEY (theme, symbol, asof)
);
"""


class Warehouse:
    """一个根目录下的数据仓：kline/*.parquet + meta.db。"""

    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser()
        self.kline_dir = self.root / "kline"
        self.pool_dir = self.root / "pool"
        self.db_path = self.root / "meta.db"
        self.kline_dir.mkdir(parents=True, exist_ok=True)
        self.pool_dir.mkdir(parents=True, exist_ok=True)

    # ---------- 建表 ----------
    def ensure_schema(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.executescript(_META_SCHEMA)

    # ---------- 日线 ----------
    def write_kline(self, df: pd.DataFrame) -> int:
        """按年写入 Parquet；同 (symbol, trade_date) 后写覆盖先写（幂等）。"""
        if df.empty:
            return 0
        df = df.copy()
        missing = set(KLINE_COLUMNS) - set(df.columns)
        for col in missing:
            df[col] = None
        df = df[KLINE_COLUMNS]
        df["trade_date"] = df["trade_date"].astype(str)
        # 价格必须为正：新浪对"代码启用前的历史日期"会返回 0 价占位行
        # （实测 920xxx 北交所代码在 2018–2020 的 189 行），这类行不可用于回测
        df = df[pd.to_numeric(df["close"], errors="coerce") > 0]
        if df.empty:
            return 0
        df["year"] = df["trade_date"].str[:4]

        written = 0
        for year, chunk in df.groupby("year"):
            path = self.kline_dir / f"{year}.parquet"
            if path.exists():
                old = pd.read_parquet(path)
                merged = pd.concat([old, chunk.drop(columns=["year"])], ignore_index=True)
            else:
                merged = chunk.drop(columns=["year"]).copy()
            merged = (merged.sort_values(["symbol", "trade_date"])
                            .drop_duplicates(["symbol", "trade_date"], keep="last"))
            merged.to_parquet(path, index=False)
            written += len(chunk)
        return written

    def read_kline(self, symbols=None, start=None, end=None) -> pd.DataFrame:
        """读日线；start/end 为 YYYY-MM-DD，闭区间。"""
        files = sorted(self.kline_dir.glob("*.parquet"))
        if not files:
            return pd.DataFrame(columns=KLINE_COLUMNS)
        df = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
        if symbols:
            df = df[df["symbol"].isin(list(symbols))]
        if start:
            df = df[df["trade_date"] >= str(start)]
        if end:
            df = df[df["trade_date"] <= str(end)]
        return df.sort_values(["symbol", "trade_date"]).reset_index(drop=True)

    # ---------- 股票池 ----------
    def write_pool(self, df: pd.DataFrame) -> int:
        """股票池按年写 Parquet：名称/板块高度重复，字典编码后体积极小。

        原先放 SQLite 时每天全量快照会产生笛卡尔积（250 天就 109 MB，
        2,100 天约 900 MB）；改 Parquet 后同样数据只有几十 MB。
        """
        if df.empty:
            return 0
        cols = ["trade_date", "symbol", "name", "board", "is_st", "is_suspended",
                "list_date", "delist_date", "total_mv", "float_mv"]
        df = df.copy()
        for c in cols:
            if c not in df.columns:
                df[c] = None
        df = df[cols]
        df["trade_date"] = df["trade_date"].astype(str)
        df["year"] = df["trade_date"].str[:4]

        written = 0
        for year, chunk in df.groupby("year"):
            path = self.pool_dir / f"{year}.parquet"
            if path.exists():
                old = pd.read_parquet(path)
                merged = pd.concat([old, chunk.drop(columns=["year"])], ignore_index=True)
            else:
                merged = chunk.drop(columns=["year"]).copy()
            merged = (merged.drop_duplicates(["trade_date", "symbol"], keep="last")
                            .sort_values(["trade_date", "symbol"]))
            merged.to_parquet(path, index=False)
            written += len(chunk)
        return written

    def read_pool(self, start=None, end=None, symbols=None) -> pd.DataFrame:
        files = sorted(self.pool_dir.glob("*.parquet"))
        if not files:
            return pd.DataFrame(columns=["trade_date", "symbol", "name"])
        df = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
        if start:
            df = df[df["trade_date"] >= str(start)]
        if end:
            df = df[df["trade_date"] <= str(end)]
        if symbols:
            df = df[df["symbol"].isin(list(symbols))]
        return df.reset_index(drop=True)

    def pool_asof(self, trade_date: str) -> pd.DataFrame:
        """当日可见的股票池快照（禁止用当前表回溯）。"""
        return self.read_pool(start=trade_date, end=trade_date)

    def upsert_pool(self, df: pd.DataFrame) -> int:
        """兼容旧接口：转调 write_pool。"""
        return self.write_pool(df)

    # ---------- 财务 ----------
    def upsert_financial(self, df: pd.DataFrame) -> int:
        if df.empty:
            return 0
        cols = ["symbol", "report_period", "publish_date", "update_time", "item", "value"]
        df = df.copy()
        for c in cols:
            if c not in df.columns:
                df[c] = None
        with sqlite3.connect(self.db_path) as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO financial_asof VALUES (?,?,?,?,?,?)",
                df[cols].itertuples(index=False, name=None),
            )
        return len(df)

    def financial_asof(self, symbol: str, asof: str) -> pd.DataFrame:
        """PIT 查询：只返回在 asof 当天或之前已公告的记录。"""
        with sqlite3.connect(self.db_path) as conn:
            return pd.read_sql_query(
                "SELECT * FROM financial_asof WHERE symbol = ? AND publish_date <= ?"
                " ORDER BY report_period DESC, item",
                conn, params=(str(symbol), str(asof)),
            )

    # ---------- 概览 ----------
    def stats(self) -> dict:
        kline_files = sorted(self.kline_dir.glob("*.parquet"))
        pool_files = sorted(self.pool_dir.glob("*.parquet"))
        rows = sum(len(pd.read_parquet(f, columns=["symbol"])) for f in kline_files)
        pool_rows = sum(len(pd.read_parquet(f, columns=["symbol"])) for f in pool_files)
        db_size = self.db_path.stat().st_size if self.db_path.exists() else 0
        return {
            "kline_files": len(kline_files),
            "kline_rows": rows,
            "kline_bytes": sum(f.stat().st_size for f in kline_files),
            "pool_files": len(pool_files),
            "pool_rows": pool_rows,
            "pool_bytes": sum(f.stat().st_size for f in pool_files),
            "db_bytes": db_size,
        }
