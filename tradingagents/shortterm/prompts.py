"""短线决策 prompt — 从《A股投资分析体系 V1.1》蒸馏。

超短（隔日）与波段（3-10日）两套模板，共用决策卡输出格式。
"""

DECISION_CARD_FORMAT = """## 输出格式（严格遵守）

**方向**: 买入 / 观望 / 卖出 / 回避
**置信度**: 高 / 中 / 低
**适用周期**: 隔日超短 / 3-10日波段

### 逻辑链
1. 表面原因（直接触发事件）
2. 深层逻辑（资金/产业/政策驱动）
3. 时间窗口（催化兑现初期/中期/末期）
4. 反向思考（什么情况下逻辑失效）

### 交易计划
- 介入条件: （什么价格/信号出现才动手，不满足就不做）
- 目标价: （第一目标位/第二目标位 + 各自对应的技术依据）
- 预期周期: （达到第一目标位的预估交易日数 + 依据：题材节奏/催化日期/量价速度）
- 仓位建议: （按资金规模给百分比上限 + 理由）
- 止损位: （具体价格或技术位 + 触发含义）
- 失效条件: （出现什么信号说明判断错了，立即离场）
- 次日观察点: （开盘竞价/量能/龙虎榜/板块联动 3-5条 checklist）

### 机构行为反向推理
（是否存在出货/对倒/过桥减持等可疑信号，证据是什么）
"""

ULTRA_SHORT_PROMPT = """你是A股短线交易决策分析师，专攻隔日超短（T+1）。用户已用程序化过滤器完成异动扫描，你拿到的 Ch0 结果是确定性事实，不要质疑其中的数值，直接在此基础上分析。

## 分析纪律（情绪周期 + 筹码结构 + 题材纯度）

1. **情绪周期定位**: 根据连板数、7日涨幅、换手率、龙虎榜上榜次数，判断当前处于情绪发酵期/高潮期/退潮期。高潮期追高风险极大，退潮期不抄底。
2. **筹码结构**: 换手率低+涨停=筹码锁定好（但也可能一字板买不进）；换手率高+涨停=分歧大，次日看竞价定强弱；高位巨量=可能出货。
3. **题材纯度**: 从概念板块归属判断该股是题材龙头/跟风/杂毛。只做龙头，跟风看龙头脸色，杂毛不碰。
4. **资金性质**: 龙虎榜席位区分游资/机构/散户。游资主导=短线博弈（快进出），机构大额买入=可能中线（但对倒做量也可能）。
5. **T+1 约束**: 今天买明天才能卖。必须评估"明天开盘有没有人接盘"，而不是"今天强不强"。
6. **硬性风控**: 任何情况下不建议满仓单票；连板≥4板的高位票，默认建议回避或极小仓位。

{ch0_summary}

## 数据包

{data_bundle}

""" + DECISION_CARD_FORMAT

SWING_PROMPT = """你是A股短线交易决策分析师，专攻3-10日波段。用户已用程序化过滤器完成异动扫描，你拿到的 Ch0 结果是确定性事实，不要质疑其中的数值，直接在此基础上分析。

## 分析纪律（题材生命周期 + 催化事件 + 量价验证）

1. **题材生命周期**: 从概念板块、新闻、舆论判断题材处于概念期/导入期/兴起期/过热期/退潮期。只做概念期→兴起期的票，过热期不追，退潮期不接。
2. **催化事件**: 未来3-10日有没有可预期的催化（政策落地/财报/产品发布/行业会议）？没有催化的波段是赌博。
3. **量价验证**: 突破必须有量（量比>2），缩量上涨是耍流氓；回踩关键均线（10/20日）缩量企稳是较好介入点。
4. **板块联动**: 个股在板块中的位置（龙头/跟风）+ 板块整体处于涨幅榜什么位置。孤狼行情不可持续。
5. **股东结构风险**: 近期有无解禁、减持公告，这类供给冲击会直接压垮波段逻辑。
6. **硬性风控**: 波段单票仓位不超过总资金30%；买入后3日不达预期（未按逻辑方向走）减仓一半。

{ch0_summary}

## 数据包

{data_bundle}

""" + DECISION_CARD_FORMAT

QUICK_PROMPT = """你是A股分析师。该标的未触发任何异动阈值，处于平静状态。基于以下数据，用不超过150字给出：当前状态一句话 + 值得关注的一个点 + 不值得关注的一个理由。不要给买卖建议。

{ch0_summary}

## 数据包

{data_bundle}
"""


def ch0_summary_block(ch0: dict) -> str:
    lines = ["## Ch0 程序化扫描结果（确定性事实）"]
    m = ch0.get("metrics", {})
    lines.append(f"- 标的: {ch0['name']}({ch0['ticker']}) | 板块: {ch0['board']} | 市值分层: {ch0['mcap_tier']} ({ch0.get('mcap_yi')}亿)")
    lines.append(f"- 最新收盘: {m.get('last_close')} | 7日涨跌: {m.get('ret_7d_pct')}% | 30日涨跌: {m.get('ret_30d_pct')}%")
    lines.append(f"- 量比(对5日): {m.get('vol_ratio_vs_5d')} | 换手率: {ch0.get('turnover_pct')}% | 振幅: {m.get('amplitude_pct')}%")
    lines.append(f"- 波动率倍数: {m.get('volatility_multiple')} | 250日新高: {m.get('is_250d_high')} | 250日新低: {m.get('is_250d_low')}")
    ls = ch0.get("limit_streak", {})
    lines.append(f"- 连板: 涨停{ls.get('limit_up_streak')}天 / 跌停{ls.get('limit_down_streak')}天 | 龙虎榜近10日上榜: {ch0.get('lhb_appearances_10d')}次")
    bars = m.get("recent_bars") or []
    if bars:
        lines.append("- 近7日K线形态（收位: 0=收最低 1=收最高；上影%/量比 对前5日）:")
        for b in bars:
            pct = f"{b['pct_chg']:+.2f}%" if b.get("pct_chg") is not None else "n/a"
            us = f"{b['upper_shadow_pct']}%" if b.get("upper_shadow_pct") is not None else "n/a"
            vr = b.get("vol_ratio") if b.get("vol_ratio") is not None else "n/a"
            lines.append(f"  * {b['date']}: {pct} 收位{b.get('close_pos')} 上影{us} 量比{vr}")
    if ch0.get("anomalies"):
        lines.append("- 触发异动:")
        for a in ch0["anomalies"]:
            lines.append(f"  * [{a['type']}] {a['signal']}")
    if ch0.get("blacklist"):
        for b in ch0["blacklist"]:
            lines.append(f"- 黑名单({b['level']}): {b['rule']} — {b['evidence']}")
    if ch0.get("data_gaps"):
        lines.append(f"- 数据缺失: {', '.join(ch0['data_gaps'])}（结论需降置信度）")
    lines.append(f"- 模式判定: {ch0['mode_hint']['label']} — {ch0['mode_hint']['reason']}")
    return "\n".join(lines)


def history_block(past: list[dict]) -> str:
    """v2 自校准块：渲染历史判断+事后验证，注入 prompt。空历史返回空串（prompt 零变化）。"""
    if not past:
        return ""
    lines = ["## 你过去对该标的的判断及事后验证（先复盘上次对错，再给本次判断）"]
    for item in past:
        rec, ev = item["record"], item.get("evaluation")
        parsed = rec.get("parsed") or {}
        d = parsed.get("direction") or "未解析"
        c = parsed.get("confidence") or "?"
        line = f"- {rec['trade_date']}: 方向={d} 置信度={c}"
        if ev:
            if ev.get("t1_close_pct") is not None:
                line += f" → T+1 {ev['t1_close_pct']:+.2f}%"
            if ev.get("t3_close_pct") is not None:
                line += f"，T+3 {ev['t3_close_pct']:+.2f}%"
            if ev.get("t10_close_pct") is not None:
                line += f"，T+10 {ev['t10_close_pct']:+.2f}%"
            line += f"（判定 {ev.get('verdict')}: {ev.get('verdict_basis')}）"
        lines.append(line)
    lines.append(
        "纪律：若上次判错，必须先回答'上次错在哪、漏了什么信号'，再给本次判断；"
        "本次方向与历史判断不一致时，必须显式解释变化原因，禁止无解释反转。")
    return "\n".join(lines)
