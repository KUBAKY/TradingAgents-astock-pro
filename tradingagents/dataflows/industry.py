"""行业中位数聚合（维度8/9/14 的数据底座）。

口径说明（重要）：
- 行业分类用**新浪行业（49 类，接近申万一级）**；申万二级与东财 BK 在本机不可得。
- 财务指标来自 baostock：roeAvg（平均净资产收益率）、gpMargin（毛利率）、npMargin（净利率）、
  YOYNI（净利同比）、liabilityToAsset（资产负债率）。
- 每条记录都带 pubDate，可用于 PIT（只使用分析日之前已公告的数据）。
- 结果按 (行业, 报告期) 缓存到 data/industry_stats/，避免重复拉取（单行业约 200–500 只，耗时 10–20 分钟）。

为什么不做全市场一次算完：baostock 单次查询约 1.4 秒，5,500 只要 6 小时以上；
按行业按需计算 + 缓存，是当前数据源下的最优解。
"""

from __future__ import annotations

import json
import re
import statistics
import time
from pathlib import Path

import requests

UA = {"User-Agent": "Mozilla/5.0"}
SINA_HY = "http://vip.stock.finance.sina.com.cn/q/view/newSinaHy.php"
SINA_MEMBERS = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
                "Market_Center.getHQNodeData?page={page}&num=100&sort=symbol&asc=1"
                "&node={node}&symbol=&_s_r_a=page")

METRICS = ["roeAvg", "gpMargin", "npMargin", "YOYNI", "liabilityToAsset"]


def fetch_sina_industries() -> dict[str, dict]:
    """→ {node: {"name": 行业名, "count": 家数}}。"""
    txt = requests.get(SINA_HY, headers=UA, timeout=20).content.decode("gbk", "ignore")
    out = {}
    for node, payload in re.findall(r'"(new_[a-z0-9]+)":"([^"]+)"', txt):
        parts = payload.split(",")
        if len(parts) >= 3 and parts[2].isdigit():
            out[node] = {"name": parts[1], "count": int(parts[2])}
    return out


def fetch_industry_members(node: str, max_pages: int = 12) -> list[str]:
    syms: list[str] = []
    for page in range(1, max_pages + 1):
        try:
            r = requests.get(SINA_MEMBERS.format(page=page, node=node), headers=UA, timeout=15)
            rows = r.json()
        except Exception:
            break
        if not isinstance(rows, list) or not rows:
            break
        syms += [x["code"] for x in rows]
        if len(rows) < 100:
            break
    return syms


def build_industry_map(cache: Path) -> dict[str, str]:
    """→ {6位代码: 行业名}，缓存到 JSON。"""
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))
    mapping: dict[str, str] = {}
    for node, meta in fetch_sina_industries().items():
        for sym in fetch_industry_members(node):
            mapping[sym] = meta["name"]
        time.sleep(0.1)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(mapping, ensure_ascii=False, indent=0), encoding="utf-8")
    return mapping


def _bs_code(symbol: str) -> str:
    s = str(symbol)
    if s.startswith("92"):            # 北交所新段，必须在 9 之前判（踩过两次）
        return f"bj.{s}"
    if s.startswith(("4", "8")):
        return f"bj.{s}"
    if s.startswith(("6", "9")):
        return f"sh.{s}"
    return f"sz.{s}"


def _query(bs, fn, code, year, quarter):
    rs = fn(code=code, year=year, quarter=quarter)
    if rs.error_code != "0":
        return None
    if rs.next():
        return dict(zip(rs.fields, rs.get_row_data()))
    return None


def collect_peer_metrics(symbols: list[str], year: int, quarter: int,
                         with_balance: bool = True, limit: int | None = None,
                         progress=None) -> list[dict]:
    """逐只取 baostock 财务指标；单只约 1.4 秒/查询。"""
    import baostock as bs

    rows: list[dict] = []
    todo = symbols[:limit] if limit else symbols
    for i, sym in enumerate(todo, 1):
        code = _bs_code(sym)
        p = _query(bs, bs.query_profit_data, code, year, quarter)
        g = _query(bs, bs.query_growth_data, code, year, quarter)
        b = _query(bs, bs.query_balance_data, code, year, quarter) if with_balance else None
        if not p:
            continue
        row = {"symbol": sym, "statDate": p.get("statDate"), "pubDate": p.get("pubDate")}
        for k in ("roeAvg", "gpMargin", "npMargin", "MBRevenue"):
            row[k] = _num(p.get(k))
        row["YOYNI"] = _num((g or {}).get("YOYNI"))
        row["liabilityToAsset"] = _num((b or {}).get("liabilityToAsset"))
        rows.append(row)
        if progress and i % 20 == 0:
            progress(i, len(todo))
        time.sleep(0.02)
    return rows


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def aggregate(rows: list[dict], period: str, industry: str) -> dict:
    """算中位数与样本量；样本 <10 标记不可用。"""
    out = {"industry": industry, "period": period, "n": len(rows), "metrics": {}}
    if len(rows) < 10:
        out["warning"] = "样本不足 10 只，中位数不可靠"
    for m in METRICS:
        vals = [r[m] for r in rows if r.get(m) is not None]
        if len(vals) >= 5:
            out["metrics"][m] = {
                "median": round(statistics.median(vals), 4),
                "p25": round(statistics.quantiles(vals, n=4)[0], 4),
                "p75": round(statistics.quantiles(vals, n=4)[2], 4),
                "n": len(vals),
            }
    return out


def industry_median(symbol: str, year: int, quarter: int,
                    data_dir: str | Path = "data", with_balance: bool = True,
                    limit: int | None = None) -> dict:
    """按股票代码查其行业中位数；命中缓存直接返回。"""
    import baostock as bs
    import contextlib
    import io

    data_dir = Path(data_dir)
    mapping = build_industry_map(data_dir / "industry_map.json")
    industry = mapping.get(str(symbol))
    if not industry:
        raise RuntimeError(f"未找到 {symbol} 的行业归属（新浪行业口径）")

    period = f"{year}Q{quarter}"
    cache = data_dir / "industry_stats" / period / f"{industry}.json"
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))

    peers = [s for s, ind in mapping.items() if ind == industry]
    with contextlib.redirect_stdout(io.StringIO()):
        bs.login()
    try:
        rows = collect_peer_metrics(peers, year, quarter,
                                    with_balance=with_balance, limit=limit)
    finally:
        with contextlib.redirect_stdout(io.StringIO()):
            bs.logout()

    result = aggregate(rows, period, industry)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def build_industry_map_baostock(cache: Path, symbols: list[str] | None = None,
                                progress=None) -> dict[str, str]:
    """用 baostock 证监会行业分类建映射（准确但慢：约 1.2 秒/只，全市场约 110 分钟）。

    为什么不用新浪行业：实测把 300308（光模块）归为「机械行业」，分类过时，
    拿来算行业中位数会得到误导性结果。宁可慢，不可错。
    """
    import contextlib
    import io

    import baostock as bs

    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))

    if symbols is None:
        from tradingagents.backtest.store import Warehouse
        symbols = sorted(Warehouse("data/warehouse").read_kline()[["symbol"]]
                         .drop_duplicates()["symbol"].tolist())

    mapping: dict[str, str] = {}
    with contextlib.redirect_stdout(io.StringIO()):
        bs.login()
    try:
        for i, sym in enumerate(symbols, 1):
            rs = bs.query_stock_industry(code=_bs_code(sym))
            if rs.error_code == "0" and rs.next():
                row = rs.get_row_data()
                if len(row) >= 4 and row[3]:
                    mapping[sym] = row[3]
            if progress and i % 200 == 0:
                progress(i, len(symbols))
    finally:
        with contextlib.redirect_stdout(io.StringIO()):
            bs.logout()

    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(mapping, ensure_ascii=False), encoding="utf-8")
    return mapping
