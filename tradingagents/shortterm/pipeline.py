"""短线分析 pipeline：Ch0 → 数据包 → 单次 LLM 决策卡。

用法:
    python -m tradingagents.shortterm.pipeline 300750
    python -m tradingagents.shortterm.pipeline 300750 --date 2026-07-31 --intent "连板想追高" --capital 100000
"""

from __future__ import annotations

import argparse
import json
import os
import uuid
from datetime import datetime, timedelta

from . import trace as _trace
from ..cost import tracker as _cost
from ..llm_clients.factory import create_llm_client
from .ch0 import run_ch0
from .anchors import check_numeric_anchors
from .assertions import check_assertions
from .history import load_past_evaluations
from .prompts import QUICK_PROMPT, SWING_PROMPT, ULTRA_SHORT_PROMPT, ch0_summary_block, history_block
from .schema import build_retry_feedback, validate_decision_card


def gather_data_bundle(ticker: str, trade_date: str, mode: str) -> str:
    from ..dataflows import a_stock

    start = (datetime.strptime(trade_date, "%Y-%m-%d") - timedelta(days=60)).strftime("%Y-%m-%d")
    sections = []

    def add(title, fn):
        try:
            out = fn()
            if isinstance(out, str) and out.strip():
                sections.append(f"### {title}\n{out[:4000]}")
        except Exception as e:
            sections.append(f"### {title}\n[数据获取失败: {e}]")

    add("近期行情", lambda: a_stock.get_stock_data(ticker, start, trade_date))
    add("技术指标", lambda: a_stock.get_indicators(ticker, "macd,rsi,kdj,boll", trade_date, 30))
    add("资金流向", lambda: a_stock.get_fund_flow(ticker, trade_date, True))
    add("龙虎榜", lambda: a_stock.get_dragon_tiger_board(ticker, trade_date, 30))
    add("概念板块归属", lambda: a_stock.get_concept_blocks(ticker))
    add("基本面快查", lambda: a_stock.get_fundamentals(ticker, trade_date))
    add("个股新闻", lambda: a_stock.get_news(ticker, start, trade_date))

    if mode == "swing":
        add("限售解禁", lambda: a_stock.get_lockup_expiry(ticker, trade_date))
        add("内部人交易", lambda: a_stock.get_insider_transactions(ticker))
        add("北向资金", lambda: a_stock.get_northbound_flow(trade_date, True))
    if mode == "ultra_short":
        add("市场情绪(热股榜)", lambda: a_stock.get_hot_stocks(trade_date))

    return "\n\n".join(sections)


class _RequestsAnthropicLLM:
    """headroom-proxy 对 httpx 502、对 urllib3 正常，故 anthropic 走 requests 直连。"""

    def __init__(self, model: str, base_url: str | None = None):
        self.model = model
        self.base_url = (base_url or os.environ.get("ANTHROPIC_BASE_URL")
                         or "https://api.anthropic.com").rstrip("/")

    class _Resp:
        def __init__(self, content: str, usage: dict | None = None):
            self.content = content
            self.usage = usage or {}

    def invoke(self, prompt: str) -> "_RequestsAnthropicLLM._Resp":
        import requests

        api_key = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
        r = requests.post(
            f"{self.base_url}/v1/messages",
            headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"},
            json={"model": self.model, "max_tokens": 8192,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=300,
        )
        r.raise_for_status()
        data = r.json()
        blocks = data.get("content", [])
        text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        if not text.strip():
            kinds = [b.get("type") for b in blocks]
            raise RuntimeError(f"LLM 返回空文本 (blocks={kinds}, stop={data.get('stop_reason')})")
        return self._Resp(text, usage=data.get("usage"))


def build_llm(provider: str, model: str, base_url: str | None):
    if provider.lower() == "anthropic":
        llm: object = _RequestsAnthropicLLM(model, base_url)
    else:
        client = create_llm_client(provider, model, base_url)
        llm = client.get_llm()
    return _cost.wrap_llm(llm, provider, model)


def run(ticker: str, trade_date: str, intent: str = "", capital: float | None = None,
        cost: float | None = None, shares: int | None = None,
        provider: str = "deepseek", model: str = "deepseek-v4-flash",
        base_url: str | None = None, ch0_only: bool = False,
        trace: bool = False, cost_feature: str = "shortterm") -> dict:

    run_id = uuid.uuid4().hex
    tokens = _cost.cost_context(cost_feature, run_id)
    try:
        return _run_impl(ticker, trade_date, intent, capital, cost, shares,
                         provider, model, base_url, ch0_only, trace, run_id)
    finally:
        _cost.reset_cost_context(tokens)  # 线程内不留残留（web job 线程/测试复用安全）


def _run_impl(ticker: str, trade_date: str, intent: str, capital: float | None,
              cost: float | None, shares: int | None, provider: str, model: str,
              base_url: str | None, ch0_only: bool, trace: bool, run_id: str) -> dict:

    ch0 = run_ch0(ticker, trade_date)

    if ch0["verdict"] == "BLACKLIST":
        report_lines = [f"# ⛔ Blacklist | {ch0['name']}({ch0['ticker']})", ""]
        for h in ch0["blacklist"]:
            report_lines.append(f"- **{h['level'].upper()}**: {h['rule']} — {h['evidence']}")
        report_lines.append(f"\n{ch0['action']}")
        return {"ch0": ch0, "mode": "blacklist", "report": "\n".join(report_lines), "bundle": None}

    if ch0_only:
        return {"ch0": ch0, "mode": "ch0_only", "report": json.dumps(ch0, ensure_ascii=False, indent=2), "bundle": None}

    mode = ch0["mode_hint"]["mode"]
    bundle = gather_data_bundle(ticker, trade_date, mode if mode in ("swing", "ultra_short") else "swing")
    ch0_block = ch0_summary_block(ch0)

    # v2 自校准：注入该票历史判断+事后验证（无历史/失败 → 零变化）
    try:
        hist = history_block(load_past_evaluations(ch0["ticker"], trade_date))
    except Exception:
        hist = ""
    if hist:
        ch0_block += "\n\n" + hist
    # v2.1 误差反馈：该票近期验证结论 + 方向级错判警示（注册表数据，无则零变化）
    try:
        from tradingagents.analysis_registry.feedback import feedback_injection_block
        fbk = feedback_injection_block(ch0["ticker"])
    except Exception:
        fbk = ""
    if fbk:
        ch0_block += "\n\n" + fbk

    extras = []
    if intent:
        extras.append(f"\n用户意图: {intent}（分析需针对性回答此诉求）")
    if capital:
        extras.append(f"\n资金规模: {capital:,.0f} 元（仓位建议需给出具体金额）")
    if cost:
        last = ch0.get("metrics", {}).get("last_close")
        pnl = f"，浮动盈亏 {(last / cost - 1) * 100:+.1f}%" if last else ""
        pos = f"，持仓 {shares} 股" if shares else ""
        extras.append(
            f"\n持仓状态: 成本价 {cost} 元{pos}，现价 {last}{pnl}"
            f"（止损位/解套位/减仓计划必须围绕成本价精确计算；"
            f"若现价低于成本，明确回答'割/持/补'三选一及理由，禁止模棱两可）"
        )
    if extras:
        ch0_block += "\n" + "\n".join(extras)

    prompt_tpl = {"ultra_short": ULTRA_SHORT_PROMPT, "swing": SWING_PROMPT}.get(mode, QUICK_PROMPT)
    prompt = prompt_tpl.replace("{ch0_summary}", ch0_block).replace("{data_bundle}", bundle)

    llm = build_llm(provider, model, base_url)
    t0 = datetime.now()
    resp = llm.invoke(prompt)
    report = resp.content if hasattr(resp, "content") else str(resp)

    # 契约校验（格式 schema + 断言红线）：违规 → 带清单反馈重试一次，仍失败标记不阻塞
    def _violations(text: str) -> list[str]:
        return validate_decision_card(text, mode) + check_assertions(text)

    violations = _violations(report)
    first_report, first_violations = report, violations
    retried = False
    if violations:
        retried = True
        try:
            resp2 = llm.invoke(prompt + build_retry_feedback(report, violations))
            report2 = resp2.content if hasattr(resp2, "content") else str(resp2)
            violations2 = _violations(report2)
            if not violations2:
                report, violations = report2, []
            else:
                violations = violations2
        except Exception:
            pass
    elapsed_ms = int((datetime.now() - t0).total_seconds() * 1000)

    # 数值锚定（软告警）：报告引用的基本面数字须能在数据源中找到
    unanchored = check_numeric_anchors(report, f"{ch0_block}\n\n{bundle}")

    validation = {"ok": not violations, "violations": violations,
                  "retried": retried, "unanchored": unanchored}

    # 执行轨迹（默认关闭，trace=True 或 ST_TRACE=1 开启；落盘失败不阻塞）
    trace_path = None
    if _trace.trace_enabled(trace):
        try:
            rec = {"ticker": ch0["ticker"], "name": ch0.get("name", ""),
                   "trade_date": trade_date, "mode": mode,
                   "provider": provider, "model": model,
                   "prompt": prompt, "response": report,
                   "validation": validation, "elapsed_ms": elapsed_ms,
                   "attempts": 2 if retried else 1}
            if retried:
                rec["first_response"] = first_report
                rec["first_violations"] = first_violations
            trace_path = str(_trace.save_trace(rec))
        except Exception:
            trace_path = None

    return {"ch0": ch0, "mode": mode, "report": report, "bundle": bundle,
            "validation": validation, "trace_path": trace_path,
            "cost": _cost.run_summary(run_id)}


def main():
    p = argparse.ArgumentParser(description="A股短线分析 agent")
    p.add_argument("ticker")
    p.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"))
    p.add_argument("--intent", default="", help="你的诉求，如 '已持仓被套' / '连板想追'")
    p.add_argument("--capital", type=float, default=None)
    p.add_argument("--cost", type=float, default=None, help="持仓成本价（被套分析时传）")
    p.add_argument("--shares", type=int, default=None, help="持仓股数")
    p.add_argument("--provider", default=os.environ.get("ST_PROVIDER", "deepseek"))
    p.add_argument("--model", default=os.environ.get("ST_MODEL", "deepseek-v4-flash"))
    p.add_argument("--base-url", default=os.environ.get("ST_BASE_URL"))
    p.add_argument("--ch0-only", action="store_true", help="只跑 Ch0 扫描，不调 LLM")
    p.add_argument("--trace", action="store_true", help="落盘执行轨迹（Prompt 全文+原始返回）")
    p.add_argument("--out", default=None, help="报告输出路径（markdown）")
    args = p.parse_args()

    result = run(args.ticker, args.date, args.intent, args.capital, args.cost, args.shares,
                 args.provider, args.model, args.base_url, args.ch0_only, trace=args.trace)

    print(result["report"])
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(result["report"])
        print(f"\n[saved] {args.out}")


if __name__ == "__main__":
    main()
