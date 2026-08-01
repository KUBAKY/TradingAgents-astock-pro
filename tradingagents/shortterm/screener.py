"""全市场活跃股扫描器（场景3：给定资金选股）。

三级漏斗：
1. 东财push2全市场快照（每板块1次请求）→ 活跃度粗排，取每板块 top N
2. run_ch0 精扫（黑名单/过热/异动/模式）→ 过滤+标注
3. 单次 LLM → 按资金规模分板块推荐 + 理由

用法:
    python -m tradingagents.shortterm.screener --capital 200000
    python -m tradingagents.shortterm.screener --capital 200000 --no-llm
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime

from ..dataflows.a_stock import _em_get
from .ch0 import run_ch0
from .pipeline import build_llm

BOARD_FS = {
    "main": "m:1+t:2,m:0+t:6,m:0+t:13",  # 沪深主板
    "cyb": "m:0+t:80",                    # 创业板
    "kcb": "m:1+t:23",                    # 科创板
}

# f62=主力净流入(元) f100=行业板块 f184=主力净占比(%) —— 随快照免费返回（实测）
_FIELDS = "f2,f3,f5,f6,f8,f10,f12,f14,f20,f21,f62,f100,f184"

# clist 页大小硬上限 100（实测 pz=5000 仍返回 100）。
# 单一 fid=f6(成交额) 排序 top100 全是权重股（实测小市值 0/100），
# f8(换手率)/f3(涨幅) 排序小市值占 60%+ → 三排序键合并去重消除结构性漏选。
SORT_KEYS = ("f6", "f8", "f3")


def fetch_board_snapshot(board: str, sort_key: str = "f6", top: int = 100) -> list[dict]:
    """东财push2 clist：单请求拿整板快照（上限100条），按 sort_key 排序。"""
    url = "https://push2.eastmoney.com/api/qt/clist/get"
    params = {
        "pn": "1", "pz": str(top), "po": "1", "np": "1",
        "fltt": "2", "invt": "2", "fid": sort_key,
        "fs": BOARD_FS[board],
        "fields": _FIELDS,
    }
    r = _em_get(url, params=params, timeout=15)
    data = (r.json().get("data") or {}).get("diff") or []
    out = []
    for row in data:
        name = row.get("f14", "")
        if "ST" in name.upper() or "退" in name:
            continue
        price = row.get("f2") or 0
        mcap = (row.get("f20") or 0) / 1e8
        if price <= 0 or not (30 <= mcap <= 3000):
            continue
        out.append({
            "code": row.get("f12"),
            "name": name,
            "price": price,
            "chg_pct": row.get("f3"),
            "turnover_pct": row.get("f8"),
            "vol_ratio": row.get("f10"),
            "amount_yi": round((row.get("f6") or 0) / 1e8, 2),
            "mcap_yi": round(mcap, 1),
            "main_net_inflow_yi": round((row.get("f62") or 0) / 1e8, 2),
            "main_net_inflow_pct": row.get("f184"),
            "industry": row.get("f100") or "",
        })
    return out


def fetch_board_merged(board: str, per_sort: int = 100) -> list[dict]:
    """f6(成交额)+f8(换手率)+f3(涨幅) 三排序各取 top per_sort，按 code 去重合并。

    消除单一成交额排序漏掉小市值活跃股的结构性偏差。
    """
    merged: dict[str, dict] = {}
    for sk in SORT_KEYS:
        for row in fetch_board_snapshot(board, sk, per_sort):
            merged.setdefault(row["code"], row)
    return list(merged.values())


def activity_rank(rows: list[dict]) -> list[dict]:
    """活跃度 = 换手率0.5 + 量比0.3 + 成交额0.2（各指标分位排名加权）。"""
    n = len(rows)
    if n == 0:
        return rows

    def pct_rank(key):
        ordered = sorted(rows, key=lambda r: r.get(key) or 0)
        rank = {id(r): i / max(n - 1, 1) for i, r in enumerate(ordered)}
        return rank

    rt, rv, ra = pct_rank("turnover_pct"), pct_rank("vol_ratio"), pct_rank("amount_yi")
    for r in rows:
        r["activity_score"] = round(
            0.5 * rt[id(r)] + 0.3 * rv[id(r)] + 0.2 * ra[id(r)], 3)
    return sorted(rows, key=lambda r: -r["activity_score"])


def scan(capital: float | None = None, per_board: int = 8,
         trade_date: str | None = None) -> dict:
    trade_date = trade_date or datetime.now().strftime("%Y-%m-%d")
    result = {"trade_date": trade_date, "capital": capital, "boards": {}, "rejected": {}}

    for board in BOARD_FS:
        snap = activity_rank(fetch_board_merged(board))
        candidates = []
        rejected = []
        for row in snap[:per_board]:
            try:
                ch0 = run_ch0(row["code"], trade_date)
            except Exception as e:
                rejected.append({"code": row["code"], "name": row["name"],
                                 "reason": f"ch0扫描失败: {str(e)[:80]}"})
                continue
            if ch0["verdict"] == "BLACKLIST":
                rules = "；".join(h["rule"] for h in ch0["blacklist"])
                rejected.append({"code": row["code"], "name": row["name"],
                                 "reason": f"黑名单: {rules}"})
                continue
            r30 = ch0["metrics"].get("ret_30d_pct")
            if r30 is not None and r30 < -30:
                rejected.append({"code": row["code"], "name": row["name"],
                                 "reason": f"30日跌幅 {r30}%，下跌趋势中的活跃=出货"})
                continue
            row["ch0"] = {
                "anomalies": [a["signal"] for a in ch0["anomalies"]],
                "overheated": any(a["type"] == "overheat" for a in ch0["anomalies"]),
                "mode": ch0["mode_hint"]["mode"],
                "ret_7d_pct": ch0["metrics"].get("ret_7d_pct"),
                "ret_30d_pct": ch0["metrics"].get("ret_30d_pct"),
                "limit_up_streak": ch0["limit_streak"]["limit_up_streak"],
                "lhb_10d": ch0.get("lhb_appearances_10d", 0),
                "data_gaps": ch0.get("data_gaps", []),
            }
            candidates.append(row)
        result["boards"][board] = candidates
        result["rejected"][board] = rejected

    return result


SCREENER_PROMPT = """你是A股短线选股顾问。以下是程序化三级扫描后的候选池（三排序键合并快照→活跃度粗排→黑名单/异动精扫）。用户资金 {capital} 元。

候选池字段说明：main_net_inflow_yi=当日主力净流入(亿元)，main_net_inflow_pct=主力净占比(%)，industry=行业板块（均为扫描时点实时快照）。

任务：沪深主板、创业板、科创板各选出不超过3只"活跃且值得操作"的标的，给出：
1. **为什么选它**（结合异动信号/行业题材/主力资金/量能，不用泛泛而谈）
2. **操作模式**（隔日超短 / 3-10日波段）与**介入方式**（追/等回踩/等竞价确认）
3. **资金分配**：总资金如何在选中标的间分配（单票仓位上限：超短15%，波段30%）
4. **误杀复核**：下方 rejected 列表是程序化剔除的票及原因，复核是否有误杀；有误杀说明理由并重新纳入，无误杀简述剔除逻辑成立

纪律：
- 标注 overheated=true 的票只能建议"等回踩"，不能建议追
- 主力大幅净流出(main_net_inflow_yi 显著为负)且上涨的票，警惕出货，降一档处理
- 数据缺失(data_gaps非空)的票降一档处理
- 没有合适标的的板块允许空仓，宁可缺不可滥

候选池 JSON：
{candidates}

已剔除列表 JSON：
{rejected}
"""


def recommend(scan_result: dict, provider: str, model: str,
              base_url: str | None = None) -> str:
    slim = {
        "trade_date": scan_result["trade_date"],
        "boards": scan_result["boards"],
    }
    prompt = (SCREENER_PROMPT
              .replace("{capital}", f"{scan_result['capital']:,.0f}" if scan_result.get("capital") else "未提供")
              .replace("{candidates}", json.dumps(slim, ensure_ascii=False, indent=1))
              .replace("{rejected}", json.dumps(scan_result.get("rejected", {}), ensure_ascii=False, indent=1)))
    llm = build_llm(provider, model, base_url)
    resp = llm.invoke(prompt)
    return resp.content if hasattr(resp, "content") else str(resp)


def main():
    p = argparse.ArgumentParser(description="A股全市场活跃股扫描选股")
    p.add_argument("--capital", type=float, default=None)
    p.add_argument("--per-board", type=int, default=8)
    p.add_argument("--date", default=None)
    p.add_argument("--provider", default="anthropic")
    p.add_argument("--model", default="claude-haiku-4-5")
    p.add_argument("--base-url", default=None)
    p.add_argument("--no-llm", action="store_true", help="只输出扫描JSON，不调LLM")
    p.add_argument("--out", default=None)
    args = p.parse_args()

    result = scan(args.capital, args.per_board, args.date)

    if args.no_llm:
        text = json.dumps(result, ensure_ascii=False, indent=1)
    else:
        text = recommend(result, args.provider, args.model, args.base_url)

    print(text)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"\n[saved] {args.out}")


if __name__ == "__main__":
    main()
