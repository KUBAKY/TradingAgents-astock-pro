"""行情与元数据取数（M1 建的仓只依赖免费直连源）。

优先级与单位约定（实测 2026-10-23）：
- 新浪日线：volume 单位是**股**，不含成交额 → 主源
- 腾讯日线：688/689 是**股**，其余是**手**（需 ×100），不含成交额 → 备源
- 东财 K 线：含成交额，但在代理环境下频繁 502/SSL 断连 → 仅尝试，失败即降级
- 名称与板块：腾讯实时快照 qt.gtimg.cn（GBK）

amount 缺失是已知限制：回测的流动性闸需要成交额时，用 `amount is None` 标记并在报告中披露，
不得用 volume×close 冒充成交额。
"""

from __future__ import annotations

import time

import pandas as pd
import requests

UA = {"User-Agent": "Mozilla/5.0"}
SINA = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
        "CN_MarketData.getKLineData?symbol={sym}&scale=240&ma=no&datalen={n}")
TENCENT = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={sym},day,,,{n},"
QUOTE = "https://qt.gtimg.cn/q={syms}"
EM_KLINE = ("https://push2his.eastmoney.com/api/qt/stock/kline/get?secid={secid}"
            "&fields1=f1,f2,f3&fields2=f51,f52,f53,f54,f55,f56,f57&klt=101&fqt=1"
            "&end=20500101&lmt={n}")
EM_LIST = ("https://push2.eastmoney.com/api/qt/clist/get?pn={pn}&pz=100&po=1&np=1"
           "&fltt=2&invt=2&fid=f12&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,"
           "m:0+t:81+s:2048&fields=f12,f14")
SINA_LIST = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
             "Market_Center.getHQNodeData?page={page}&num=100&sort=symbol&asc=1"
             "&node=hs_a&symbol=&_s_r_a=page")

# 覆盖各板块的种子标的：主板 / 中小板 / 创业板 / 科创板 / 北交所
SEED_SYMBOLS = [
    "600519", "601318", "600036", "601919", "601899", "601857", "603259",
    "000001", "000002", "002594", "002475", "002821", "300750", "300347",
    "688981", "688012", "688036", "920819",
]


def sina_prefix(symbol: str) -> str:
    """6 位代码 → 新浪/腾讯前缀：sh / sz / bj。"""
    s = str(symbol)
    if s.startswith("92"):          # 北交所新段（920xxx），必须在 9 之前判
        return "bj"
    if s.startswith(("4", "8")):
        return "bj"
    if s.startswith("6") or s.startswith("9"):   # 9xxxxx 只剩沪市 B 股
        return "sh"
    if s.startswith(("0", "2", "3")):
        return "sz"
    return "sh"


def board_of(symbol: str) -> str:
    s = str(symbol)
    if s.startswith("688") or s.startswith("689"):
        return "科创板"
    if s.startswith("300") or s.startswith("301"):
        return "创业板"
    if s.startswith(("8", "4", "9")):
        return "北交所"
    if s.startswith("6"):
        return "沪主板"
    return "深主板"


def _get(url: str, timeout: int = 15, retries: int = 3):
    last = None
    for i in range(retries):
        try:
            r = requests.get(url, headers=UA, timeout=timeout)
            if r.status_code == 200 and r.content:
                return r
            last = RuntimeError(f"HTTP {r.status_code}")
        except Exception as e:  # 代理下 SSL 断连是常态
            last = e
        time.sleep(0.8 * (i + 1))
    raise last


def _from_sina(symbol: str, n: int) -> pd.DataFrame:
    sym = f"{sina_prefix(symbol)}{symbol}"
    rows = _get(SINA.format(sym=sym, n=n)).json()
    if not rows:
        raise ValueError("空数据")
    df = pd.DataFrame(rows)
    return pd.DataFrame({
        "symbol": symbol,
        "trade_date": df["day"],
        "open": df["open"].astype(float),
        "high": df["high"].astype(float),
        "low": df["low"].astype(float),
        "close": df["close"].astype(float),
        "volume": df["volume"].astype(float),       # 股
        "source": "sina",
    })


def _from_tencent(symbol: str, n: int) -> pd.DataFrame:
    sym = f"{sina_prefix(symbol)}{symbol}"
    data = _get(TENCENT.format(sym=sym, n=n)).json()["data"][sym]
    rows = data.get("day") or data.get("qfqday") or []
    if not rows:
        raise ValueError("空数据")
    df = pd.DataFrame(rows).iloc[:, :6]
    df.columns = ["trade_date", "open", "close", "high", "low", "volume"]
    vol = df["volume"].astype(float)
    if not str(symbol).startswith(("688", "689")):
        vol = vol * 100                              # 手 → 股
    return pd.DataFrame({
        "symbol": symbol,
        "trade_date": df["trade_date"],
        "open": df["open"].astype(float),
        "high": df["high"].astype(float),
        "low": df["low"].astype(float),
        "close": df["close"].astype(float),
        "volume": vol,
        "source": "tencent",
    })


def _from_eastmoney(symbol: str, n: int) -> pd.DataFrame:
    market = 1 if sina_prefix(symbol) == "sh" else 0
    d = _get(EM_KLINE.format(secid=f"{market}.{symbol}", n=n), retries=1).json()["data"]
    rows = [line.split(",") for line in d["klines"]]
    return pd.DataFrame({
        "symbol": symbol,
        "trade_date": [r[0] for r in rows],
        "open": [float(r[1]) for r in rows],
        "close": [float(r[2]) for r in rows],
        "high": [float(r[3]) for r in rows],
        "low": [float(r[4]) for r in rows],
        "volume": [float(r[5]) * 100 for r in rows],   # 东财为手
        "amount": [float(r[6]) for r in rows],         # 元
        "source": "eastmoney",
    })


def fetch_daily(symbol: str, datalen: int = 250) -> pd.DataFrame:
    """取单只日线：新浪 → 东财(带成交额，1 次尝试) → 腾讯。失败抛 RuntimeError。"""
    errors = []
    for fn in (_from_sina, _from_eastmoney, _from_tencent):
        try:
            df = fn(symbol, datalen)
            for col in ("pre_close", "amount", "adj_factor"):
                if col not in df.columns:
                    df[col] = None
            df["is_suspended"] = 0
            return df
        except Exception as e:
            errors.append(f"{fn.__name__}:{type(e).__name__}")
    raise RuntimeError(f"{symbol} 全部数据源失败 ({', '.join(errors)})")


def fetch_names(symbols) -> dict[str, str]:
    """腾讯快照取简称（GBK）。返回 {symbol: name}，失败项缺省。"""
    out: dict[str, str] = {}
    syms = [f"{sina_prefix(s)}{s}" for s in symbols]
    for i in range(0, len(syms), 50):
        batch = syms[i:i + 50]
        try:
            raw = _get(QUOTE.format(syms=",".join(batch))).content.decode("gbk", "ignore")
        except Exception:
            continue
        for line in raw.split(";"):
            if "=" not in line or "~" not in line:
                continue
            parts = line.split("~")
            if len(parts) > 2:
                # 腾讯快照对部分名称做定宽补位（如「万  科Ａ」），统一去空白
                name = "".join(parts[1].split())
                out[parts[2]] = name
    return out


def fetch_market_snapshot(max_pages: int = 80) -> pd.DataFrame:
    """新浪全市场快照：代码 + 名称 + 当日成交量(股) + 当日成交额(元)。

    实测 2026-10-23：55 页 ×100 + 76 = 5,576 只。这也是唯一稳定可用的全市场列表源
    （东财 clist 在本机返回空）。
    """
    rows: list[dict] = []
    for page in range(1, max_pages + 1):
        try:
            data = _get(SINA_LIST.format(page=page), retries=2).json()
        except Exception:
            break
        if not isinstance(data, list) or not data:
            break
        rows += data
    if not rows:
        return pd.DataFrame(columns=["symbol", "name", "volume", "amount"])
    df = pd.DataFrame(rows)
    df["symbol"] = df["code"].astype(str).str.zfill(6)
    df["name"] = df["name"].astype(str).str.replace(r"\s+", "", regex=True)
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce")
    df["amount"] = pd.to_numeric(df["amount"], errors="coerce")
    return (df[["symbol", "name", "volume", "amount"]]
            .drop_duplicates("symbol").reset_index(drop=True))


def fetch_universe(limit: int = 6000) -> list[str]:
    """全市场代码表：东财优先，失败改用新浪快照；都失败返回空。"""
    codes: list[str] = []
    page = 1
    while len(codes) < limit:
        try:
            d = _get(EM_LIST.format(pn=page), retries=1).json().get("data") or {}
            rows = d.get("diff") or []
        except Exception:
            break
        if not rows:
            break
        codes += [r["f12"] for r in rows]
        page += 1
        if page > 80:
            break
    if codes:
        return codes[:limit]
    snap = fetch_market_snapshot()
    return snap["symbol"].tolist()[:limit]
