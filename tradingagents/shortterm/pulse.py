"""盘面信号 → 主线分析师的市场脉搏文本（#1 融合，零 LLM 成本）。

主线 propagate 时把短线 ch0 异动精扫 + 市场情绪温度计格式化为
system prompt 可读文本，注入 social/policy analyst。

- 纯数据计算（ch0 无 LLM 调用），单票约 +10-20s 数据请求。
- 任何失败降级返回 ""，不阻塞主线。
"""

import logging
import re

logger = logging.getLogger(__name__)

_CODE_RE = re.compile(r"^\d{6}$")

_ANOMALY_LEVELS = {
    "retail_takeover": "高危·散户接力",
    "margin_risk": "高危·融资杠杆",
    "overheat": "警示·情绪过热",
    "volume_breakout": "信号·放量突破",
    "shrink_pullback": "信号·缩量回调",
    "price_volume_divergence": "警示·量价背离",
    "volume_stagnation": "警示·量能萎缩",
    "strong_seal": "信号·强封板",
    "limit_up": "信号·连续涨停",
}


def _to_code(ticker: str) -> str:
    """输入归一化为 6 位代码；失败抛异常由调用方降级。"""
    from tradingagents.dataflows.a_stock import resolve_ticker

    s = str(ticker).strip()
    if _CODE_RE.match(s):
        return s
    return resolve_ticker(s)


def _sentiment_text(trade_date: str) -> str:
    from tradingagents.shortterm.screener import fetch_market_sentiment

    try:
        s = fetch_market_sentiment(trade_date)
    except Exception as e:
        logger.warning("market sentiment failed for %s: %s", trade_date, e)
        return ""
    if s.get("sentiment") != "ok":
        return ""
    return (f"情绪温度计: {s.get('label', '未知')} "
            f"(涨停 {s.get('limit_up_count')} 家 | 最高连板 {s.get('max_streak')} 板 | "
            f"跌停 {s.get('limit_down_count')} 家)")


def _ch0_text(code: str, trade_date: str) -> str:
    from tradingagents.shortterm.ch0 import run_ch0

    try:
        ch0 = run_ch0(code, trade_date)
    except Exception as e:
        logger.warning("ch0 pulse failed for %s: %s", code, e)
        return ""

    lines = []
    name = ch0.get("name") or ""
    board = ch0.get("board") or ""
    mcap_tier = ch0.get("mcap_tier") or ""
    lines.append(f"目标: {code} {name} | 板块: {board} | 市值分层: {mcap_tier}")

    verdict = ch0.get("verdict")
    if verdict == "BLACKLIST":
        rules = "; ".join(h.get("rule", "") for h in ch0.get("blacklist") or [])
        lines.append(f"⚠️ 黑名单: {rules}")
        return "\n".join(lines)

    mode = ch0.get("mode_hint") or {}
    lines.append(f"异动判定: {mode.get('label', '无异动')} "
                 f"(模式: {mode.get('mode', 'quiet')}) — {mode.get('reason', '')}")

    streak = (ch0.get("limit_streak") or {})
    lh = ch0.get("lhb_appearances_10d") or 0
    lines.append(f"连板: {streak.get('limit_up_streak', 0)} 板 | "
                 f"换手率: {ch0.get('turnover_pct')}% | 近10日龙虎榜: {lh} 次")

    margin_pct = (ch0.get("metrics") or {}).get("margin_to_float_mcap_pct")
    if margin_pct is not None:
        flag = " ⚠️ 超警戒线 15%" if float(margin_pct) > 15 else ""
        lines.append(f"融资余额/流通市值: {margin_pct}%{flag}")

    anomalies = ch0.get("anomalies") or []
    if anomalies:
        lines.append("异动信号:")
        for a in anomalies[:8]:
            label = _ANOMALY_LEVELS.get(a.get("type"), f"{a.get('type')}")
            lines.append(f"- [{label}] {a.get('signal', '')}")

    inst = ch0.get("institutional_flow")
    if inst:
        lines.append(f"机构动向: 买入 {inst['buy_yi']} 亿 / 卖出 {inst['sell_yi']} 亿 / "
                     f"净 {inst['net_yi']} 亿（龙虎榜机构席位估算）")

    gaps = ch0.get("data_gaps") or []
    if gaps:
        lines.append(f"数据缺失: {', '.join(gaps[:5])}")

    return "\n".join(lines)


def build_market_pulse(ticker: str, trade_date: str, *, max_chars: int = 2000) -> str:
    """构建市场脉搏文本；失败/数据全缺返回 ""。"""
    try:
        code = _to_code(ticker)
    except Exception as e:
        logger.warning("market pulse ticker resolve failed %r: %s", ticker, e)
        return ""

    parts = []
    ch0_text = _ch0_text(code, trade_date)
    if ch0_text:
        parts.append(f"【短线异动精扫 · {trade_date}】\n{ch0_text}")
    senti = _sentiment_text(trade_date)
    if senti:
        parts.append(f"【全市场情绪 · {trade_date}】\n{senti}")

    if not parts:
        return ""

    body = "\n\n".join(parts)
    header = ("以下为短线盘面信号（供情绪/政策判断参考；基本面仍以你的独立分析为准）：\n"
              "==============================\n")
    pulse = header + body
    if len(pulse) > max_chars:
        pulse = pulse[:max_chars] + "\n...[截断]"
    return pulse
