"""Ch0: 标的异动扫描 + 黑名单前置过滤（纯代码，无 LLM）。

输入 ticker + trade_date，输出结构化 JSON：
- blacklist: 命中即拒，下游不烧 LLM
- anomalies: 按板块/市值分层阈值扫描出的异动信号
- mode_hint: 隔日超短 vs 3-10日波段 的启发式判定
- data_gaps: 缺失数据源声明（降置信度用）
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Any

import pandas as pd

from ..dataflows.a_stock import (
    _eastmoney_datacenter,
    _load_ohlcv_astock,
    _normalize_ticker,
    _tencent_quote,
)

# ---------------------------------------------------------------------------
# 板块 / 市值分层
# ---------------------------------------------------------------------------

BOARD_RULES = {
    # board: (limit_pct, ret7_up, ret7_down)
    "main": (10.0, 20.0, -15.0),
    "cyb": (20.0, 30.0, -20.0),   # 创业板
    "kcb": (20.0, 30.0, -20.0),   # 科创板
    "bj": (30.0, 40.0, -30.0),    # 北交所
    "st": (5.0, 10.0, -8.0),
}

MCAP_TIERS = ["micro", "small", "mid", "large"]  # <50, 50-100, 100-500, >500 亿

# 换手率异常阈值（上限, 下限），按市值分层
TURNOVER_THRESHOLDS = {
    "large": (8.0, 0.5),
    "mid": (12.0, 0.5),
    "small": (18.0, 0.5),
    "micro": (25.0, 0.5),
    "st": (5.0, 0.0),
}


def detect_board(code: str, name: str = "") -> str:
    if "ST" in name.upper() or "退" in name:
        return "st"
    if code.startswith("688"):
        return "kcb"
    if code.startswith("300"):
        return "cyb"
    if code.startswith(("8", "43", "92")):
        return "bj"
    return "main"


def classify_mcap(mcap_yi: float, board: str = "main") -> str:
    if board == "st":
        return "st"
    if mcap_yi < 50:
        return "micro"
    if mcap_yi < 100:
        return "small"
    if mcap_yi < 500:
        return "mid"
    return "large"


# ---------------------------------------------------------------------------
# 量价计算
# ---------------------------------------------------------------------------

def compute_recent_bars(df: pd.DataFrame, n: int = 7) -> list[dict[str, Any]]:
    """末 n 根日K形态：供 LLM 识别断板/炸板/长上影/连续阳线。

    每根: date/pct_chg(对前收)/close_pos(收盘位置 0=收最低 1=收最高)/
    upper_shadow_pct(上影线相对前收%)/vol_ratio(对前5日均量)。
    """
    df = df.sort_values("Date").reset_index(drop=True)
    close = df["Close"].astype(float)
    vol = df["Volume"].astype(float)
    out = []
    for i in range(max(1, len(df) - n), len(df)):
        row = df.iloc[i]
        o, h, l, c = (float(row["Open"]), float(row["High"]),
                      float(row["Low"]), float(row["Close"]))
        prev_c = float(close.iloc[i - 1])
        rng = h - l
        prior5 = vol.iloc[max(0, i - 5):i]
        prior5_mean = float(prior5.mean()) if len(prior5) else 0.0
        out.append({
            "date": str(row["Date"])[:10],
            "pct_chg": round((c / prev_c - 1) * 100, 2) if prev_c else None,
            "close_pos": round((c - l) / rng, 2) if rng > 0 else 0.5,
            "upper_shadow_pct": round((h - max(o, c)) / prev_c * 100, 2) if prev_c else None,
            "vol_ratio": round(float(vol.iloc[i]) / prior5_mean, 2) if prior5_mean > 0 else None,
        })
    return out


def compute_price_metrics(df: pd.DataFrame) -> dict[str, Any]:
    """df: _load_ohlcv_astock 输出 (Date/Open/High/Low/Close/Volume)，已按日期过滤。"""
    df = df.sort_values("Date").reset_index(drop=True)
    if len(df) < 21:
        return {"error": f"历史数据不足 ({len(df)} bars)"}

    close = df["Close"].astype(float)
    vol = df["Volume"].astype(float)
    high = df["High"].astype(float)
    low = df["Low"].astype(float)

    last = df.iloc[-1]
    prev_close = float(close.iloc[-2])

    ret_7d = (float(close.iloc[-1]) / float(close.iloc[-8]) - 1) * 100 if len(df) >= 8 else None
    ret_30d = (float(close.iloc[-1]) / float(close.iloc[-31]) - 1) * 100 if len(df) >= 31 else None

    vol_ma5 = float(vol.iloc[-6:-1].mean())
    vol_ratio = float(vol.iloc[-1]) / vol_ma5 if vol_ma5 > 0 else None

    amplitude = (float(last["High"]) - float(last["Low"])) / prev_close * 100 if prev_close else None

    logret = close.pct_change().dropna()
    vol20_ann = float(logret.iloc[-20:].std() * math.sqrt(252) * 100)
    vol_hist_ann = float(logret.std() * math.sqrt(252) * 100)

    window = min(len(df), 250)
    close_250_high = float(close.iloc[-window:].max())
    close_250_low = float(close.iloc[-window:].min())
    prev_250_high = float(close.iloc[-window:-1].max()) if window > 1 else close_250_high
    prev_250_low = float(close.iloc[-window:-1].min()) if window > 1 else close_250_low
    last_close = float(close.iloc[-1])

    return {
        "last_date": str(last["Date"])[:10],
        "last_close": round(last_close, 3),
        "ret_7d_pct": round(ret_7d, 2) if ret_7d is not None else None,
        "ret_30d_pct": round(ret_30d, 2) if ret_30d is not None else None,
        "vol_ratio_vs_5d": round(vol_ratio, 2) if vol_ratio is not None else None,
        "amplitude_pct": round(amplitude, 2) if amplitude is not None else None,
        "volatility_20d_ann_pct": round(vol20_ann, 1),
        "volatility_hist_ann_pct": round(vol_hist_ann, 1),
        "volatility_multiple": round(vol20_ann / vol_hist_ann, 2) if vol_hist_ann > 0 else None,
        "is_250d_high": bool(last_close >= prev_250_high * 0.999),
        "is_250d_low": bool(last_close <= prev_250_low * 1.001),
        "recent_bars": compute_recent_bars(df),
        "bars": len(df),
    }


def detect_limit_streak(df: pd.DataFrame, limit_pct: float) -> dict[str, int]:
    """连续涨停/跌停天数（收盘价涨幅触及 limit_pct-0.5 视为板）。"""
    close = df["Close"].astype(float).reset_index(drop=True)
    pct = close.pct_change() * 100
    thresh = limit_pct - 0.5

    up = down = 0
    for v in reversed(pct.iloc[1:].tolist()):
        if v >= thresh:
            up += 1
        else:
            break
    for v in reversed(pct.iloc[1:].tolist()):
        if v <= -thresh:
            down += 1
        else:
            break
    return {"limit_up_streak": up, "limit_down_streak": down}


def estimate_turnover(df: pd.DataFrame, float_shares: float | None) -> float | None:
    """最后一日换手率 %。float_shares 用腾讯实时流通市值/现价近似（回看时有误差）。"""
    if not float_shares or float_shares <= 0:
        return None
    last_vol = float(df["Volume"].iloc[-1])
    return round(last_vol / float_shares * 100, 2)


# ---------------------------------------------------------------------------
# 异动扫描
# ---------------------------------------------------------------------------

def scan_anomalies(metrics: dict, board: str, mcap_tier: str,
                   turnover_pct: float | None, limit: dict) -> list[dict]:
    out = []
    limit_pct, ret7_up, ret7_down = BOARD_RULES[board]

    r7 = metrics.get("ret_7d_pct")
    if r7 is not None and (r7 > ret7_up or r7 < ret7_down):
        out.append({"type": "price", "signal": f"7日涨跌幅 {r7}% 超过板块阈值 ({ret7_up}/{ret7_down})"})

    r30 = metrics.get("ret_30d_pct")
    if r30 is not None and r30 > 50:
        out.append({"type": "overheat",
                    "signal": f"30日涨幅 {r30}% > 50%，过热：禁止追买，仅允许回踩企稳后介入"})

    amp = metrics.get("amplitude_pct")
    if amp is not None and amp > 15:
        out.append({"type": "amplitude", "signal": f"单日振幅 {amp}% > 15%"})

    vr = metrics.get("vol_ratio_vs_5d")
    if vr is not None:
        thresh = 3.0 if mcap_tier == "micro" else 2.0
        if vr > thresh:
            out.append({"type": "volume", "signal": f"成交量为5日均量 {vr} 倍 (> {thresh}x)"})

    if turnover_pct is not None and mcap_tier in TURNOVER_THRESHOLDS:
        hi, lo = TURNOVER_THRESHOLDS[mcap_tier]
        if turnover_pct > hi or (lo > 0 and turnover_pct < lo):
            out.append({"type": "turnover", "signal": f"换手率 {turnover_pct}% 超出 [{lo}, {hi}] 区间"})

    vm = metrics.get("volatility_multiple")
    if vm is not None:
        thresh = 1.5 if mcap_tier == "micro" else 2.0
        if vm > thresh:
            out.append({"type": "volatility", "signal": f"20日波动率为历史均值 {vm} 倍"})

    if metrics.get("is_250d_high"):
        out.append({"type": "breakout", "signal": "创250日新高"})
    if metrics.get("is_250d_low"):
        out.append({"type": "breakdown", "signal": "创250日新低"})

    if limit["limit_up_streak"] >= 2:
        out.append({"type": "limit_up", "signal": f"连续涨停 {limit['limit_up_streak']} 天 ({limit_pct}%板)"})
    if limit["limit_down_streak"] >= 2:
        out.append({"type": "limit_down", "signal": f"连续跌停 {limit['limit_down_streak']} 天"})

    return out


# ---------------------------------------------------------------------------
# 黑名单
# ---------------------------------------------------------------------------

def check_blacklist(name: str, df: pd.DataFrame, trade_date: str) -> list[dict]:
    hits = []
    upper = name.upper()
    if "退" in name:
        hits.append({"level": "red", "rule": "退市整理期/退市风险", "evidence": f"名称: {name}"})
    if "ST" in upper:
        hits.append({"level": "red", "rule": "ST股", "evidence": f"名称: {name}"})

    if not df.empty:
        last_bar = pd.to_datetime(df["Date"].iloc[-1])
        gap_days = (pd.to_datetime(trade_date) - last_bar).days
        if gap_days > 30:
            hits.append({"level": "red", "rule": "停牌>30天",
                         "evidence": f"最后交易日 {str(last_bar)[:10]}，距今 {gap_days} 天"})

        first_bar = pd.to_datetime(df["Date"].iloc[0])
        listing_days = (pd.to_datetime(trade_date) - first_bar).days
        if listing_days < 365:
            hits.append({"level": "yellow", "rule": "上市<1年，历史数据不足",
                         "evidence": f"首个交易日 {str(first_bar)[:10]}"})
    return hits


# ---------------------------------------------------------------------------
# 模式判定
# ---------------------------------------------------------------------------

def decide_mode(anomalies: list[dict], metrics: dict, lhb_count_10d: int) -> dict:
    types = {a["type"] for a in anomalies}
    r7 = metrics.get("ret_7d_pct") or 0

    if "limit_up" in types or (r7 > 30 and lhb_count_10d > 0):
        return {"mode": "ultra_short", "label": "隔日超短",
                "reason": "连板/7日涨幅>30%+龙虎榜活跃 → 情绪周期+筹码结构+题材纯度"}
    if "limit_down" in types:
        return {"mode": "ultra_short", "label": "隔日超短(风险)",
                "reason": "连续跌停 → 流动性危机处理，博反弹需极谨慎"}
    return {"mode": "swing", "label": "3-10日波段",
            "reason": "无极端情绪信号 → 题材生命周期+催化事件+量价验证"}


# ---------------------------------------------------------------------------
# 龙虎榜
# ---------------------------------------------------------------------------

def count_lhb_appearances(code: str, trade_date: str, days: int = 10) -> int:
    start = (datetime.strptime(trade_date, "%Y-%m-%d") - timedelta(days=days)).strftime("%Y-%m-%d")
    try:
        data = _eastmoney_datacenter(
            "RPT_DAILYBILLBOARD_DETAILSNEW",
            filter_str=(
                f"(TRADE_DATE>='{start}')(TRADE_DATE<='{trade_date}')"
                f"(SECURITY_CODE=\"{code}\")"
            ),
            page_size=50,
        )
        return len(data)
    except Exception:
        return -1  # 查询失败


def count_lhb_batch(codes: list[str], trade_date: str, days: int = 10) -> dict[str, int] | None:
    """批量统计多票近 days 天龙虎榜上榜次数：整段榜单一次拉取，本地按 code 计数。

    逐票查询要 N 次限流调用（screener 24-45 票 → 30-60s），批量只需 2-3 次。
    失败返回 None（调用方应回退逐票查询）。
    """
    start = (datetime.strptime(trade_date, "%Y-%m-%d") - timedelta(days=days)).strftime("%Y-%m-%d")
    counts: dict[str, int] = {c: 0 for c in codes}
    wanted = set(codes)
    page = 1
    try:
        while True:
            data = _eastmoney_datacenter(
                "RPT_DAILYBILLBOARD_DETAILSNEW",
                filter_str=f"(TRADE_DATE>='{start}')(TRADE_DATE<='{trade_date}')",
                page_size=500,
                page=page,
            )
            if not data:
                break
            for row in data:
                code = row.get("SECURITY_CODE")
                if code in wanted:
                    counts[code] += 1
            if len(data) < 500:
                break
            page += 1
        return counts
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def run_ch0(ticker: str, trade_date: str,
            quote: dict | None = None, lhb_count: int | None = None) -> dict[str, Any]:
    """quote/lhb_count 为可选预取注入（screener 批量场景消除逐票请求）；None 时走原有逐票获取。"""
    code = _normalize_ticker(ticker)
    data_gaps: list[str] = []

    if quote is None:
        quote = {}
        try:
            quote = _tencent_quote([code]).get(code, {})
        except Exception:
            data_gaps.append("tencent_quote")

    name = quote.get("name", "")
    board = detect_board(code, name)
    mcap_yi = float(quote.get("mcap_yi") or 0)
    mcap_tier = classify_mcap(mcap_yi, board)

    df = pd.DataFrame()
    try:
        df = _load_ohlcv_astock(code, trade_date)
    except Exception as e:
        data_gaps.append(f"ohlcv: {e}")

    blacklist = check_blacklist(name, df, trade_date)
    if any(h["level"] == "red" for h in blacklist):
        return {
            "ticker": code, "name": name, "trade_date": trade_date,
            "verdict": "BLACKLIST", "blacklist": blacklist,
            "action": "直接输出结论，跳过 Ch1-Ch7，不调用 LLM",
            "data_gaps": data_gaps,
        }

    metrics = compute_price_metrics(df) if not df.empty else {"error": "no ohlcv"}
    limit = detect_limit_streak(df, BOARD_RULES[board][0]) if not df.empty else {"limit_up_streak": 0, "limit_down_streak": 0}

    float_shares = None
    if quote.get("float_mcap_yi") and quote.get("price"):
        float_shares = quote["float_mcap_yi"] * 1e8 / quote["price"]
    turnover = estimate_turnover(df, float_shares) if not df.empty else None
    if turnover is None and not df.empty:
        data_gaps.append("float_shares(换手率无法计算)")
    if not df.empty and str(df["Date"].iloc[-1])[:10] < trade_date:
        data_gaps.append("换手率为近似值（流通股按当前值）")

    if lhb_count is None:
        lhb_10d = count_lhb_appearances(code, trade_date, 10)
    else:
        lhb_10d = lhb_count
    if lhb_10d < 0:
        data_gaps.append("dragon_tiger_board")

    anomalies = scan_anomalies(metrics, board, mcap_tier, turnover, limit)
    mode = decide_mode(anomalies, metrics, max(lhb_10d, 0))

    if not anomalies:
        mode = {"mode": "quiet", "label": "无异动",
                "reason": "未触发任何异动阈值；若用户无明确诉求，建议快速模式输出一句话结论"}

    return {
        "ticker": code, "name": name, "trade_date": trade_date,
        "verdict": "PASS",
        "board": board, "mcap_tier": mcap_tier, "mcap_yi": mcap_yi,
        "blacklist": blacklist,
        "metrics": metrics,
        "limit_streak": limit,
        "turnover_pct": turnover,
        "lhb_appearances_10d": max(lhb_10d, 0),
        "anomalies": anomalies,
        "mode_hint": mode,
        "data_gaps": data_gaps,
    }


if __name__ == "__main__":
    import json
    import sys

    t = sys.argv[1] if len(sys.argv) > 1 else "300750"
    d = sys.argv[2] if len(sys.argv) > 2 else datetime.now().strftime("%Y-%m-%d")
    print(json.dumps(run_ch0(t, d), ensure_ascii=False, indent=2))
