"""LLM 调用成本包装器 + 用量提取。

- wrap_llm(llm, provider, model)：包装 invoke/ainvoke/with_structured_output/bind，
  每次调用提取 token 用量 → 按定价表计算人民币成本 → 落账本。
- 内部任何异常静默（cost 记录失败绝不阻塞/改变 LLM 调用）。
- feature/run_id 走 contextvar（线程隔离）：pipeline 设 "shortterm"，图分析默认 "main_graph"。
"""

from __future__ import annotations

import contextvars
import time
from typing import Any, Optional

from . import ledger, pricing

_feature_var: contextvars.ContextVar[str] = contextvars.ContextVar("cost_feature", default="main_graph")
_run_var: contextvars.ContextVar[str] = contextvars.ContextVar("cost_run_id", default="")


class _CtxToken:
    def __init__(self, var, tok):
        self.var = var
        self.tok = tok


def cost_context(feature: Optional[str] = None, run_id: Optional[str] = None) -> list[_CtxToken]:
    """设置 feature/run_id（None 表示不动），返回 token 列表供 reset_cost_context 恢复。"""
    tokens = []
    if feature is not None:
        tokens.append(_CtxToken(_feature_var, _feature_var.set(feature)))
    if run_id is not None:
        tokens.append(_CtxToken(_run_var, _run_var.set(run_id)))
    return tokens


def reset_cost_context(tokens: list[_CtxToken]) -> None:
    for t in reversed(tokens or []):
        t.var.reset(t.tok)


def current_feature() -> str:
    return _feature_var.get()


def current_run_id() -> str:
    return _run_var.get()


def _to_int(v) -> Optional[int]:
    try:
        if v is None:
            return None
        return int(v)
    except (TypeError, ValueError):
        return None


def _from_raw_tokens(d: dict) -> Optional[dict]:
    input_ = _to_int(d.get("prompt_tokens", d.get("input_tokens")))
    output = _to_int(d.get("completion_tokens", d.get("output_tokens")))
    if input_ is None and output is None:
        return None
    hit = _to_int(d.get("prompt_cache_hit_tokens",
                        d.get("cache_read", d.get("cache_read_input_tokens"))))
    miss = _to_int(d.get("prompt_cache_miss_tokens",
                         d.get("cache_creation", d.get("cache_creation_input_tokens"))))
    if hit is None and miss is None and input_ is not None:
        hit, miss = 0, input_  # 无缓存拆分信息 → 全部按未命中计（保守）
    return {"input": input_, "output": output, "cache_hit": hit, "cache_miss": miss}


def extract_usage(result: Any) -> Optional[dict]:
    """从 langchain/raw 响应提取用量。取不到 → None（不记账）。"""
    if result is None:
        return None
    # 1) OpenAI 兼容原始字段（DeepSeek 缓存命中/未命中在此）
    rm = getattr(result, "response_metadata", None)
    if isinstance(rm, dict) and isinstance(rm.get("token_usage"), dict):
        u = _from_raw_tokens(rm["token_usage"])
        if u:
            return u
    # 2) langchain 规范化 usage_metadata
    um = getattr(result, "usage_metadata", None)
    if isinstance(um, dict):
        details = um.get("input_token_details") or {}
        u = _from_raw_tokens({**um, **details})
        if u:
            return u
    # 3) 直连响应挂载的 usage 属性（_RequestsAnthropicLLM 等）
    usage = getattr(result, "usage", None)
    if isinstance(usage, dict):
        u = _from_raw_tokens(usage)
        if u:
            return u
    return None


def compute_cost(usage: Optional[dict], price: Optional[dict]) -> Optional[float]:
    """按 元/百万 tokens 计算人民币成本。usage 或 price 缺失 → None（未定价）。"""
    if not usage or not price:
        return None
    hit = usage.get("cache_hit") or 0
    miss = usage.get("cache_miss")
    if miss is None:
        total_in = usage.get("input")
        miss = 0 if total_in is None else total_in - hit
    output = usage.get("output") or 0
    cost = (hit * price["input_hit"] + miss * price["input_miss"]
            + output * price["output"]) / 1_000_000
    return round(cost, 6)


class TrackedLLM:
    """透明包装 langchain 风格 LLM：记录成本，行为/属性全透传。"""

    def __init__(self, llm: Any, provider: str, model: str):
        self._wrapped = llm
        self.provider = provider
        self.model = model

    def __getattr__(self, name: str) -> Any:
        return getattr(self._wrapped, name)

    def _record(self, result: Any, start: float) -> Any:
        try:
            usage = extract_usage(result)
            cost = compute_cost(usage, pricing.get_price(self.model))
            ledger.append_entry({
                "feature": current_feature(),
                "run_id": current_run_id(),
                "provider": self.provider,
                "model": self.model,
                "input": usage.get("input") if usage else None,
                "output": usage.get("output") if usage else None,
                "cache_hit": usage.get("cache_hit") if usage else None,
                "cache_miss": usage.get("cache_miss") if usage else None,
                "cost_cny": cost,
                "elapsed_ms": int((time.time() - start) * 1000),
            })
        except Exception:
            pass  # 记账失败绝不阻塞 LLM 调用
        return result

    def invoke(self, input, config=None, **kwargs):
        start = time.time()
        result = self._wrapped.invoke(input, config=config, **kwargs)
        return self._record(result, start)

    async def ainvoke(self, input, config=None, **kwargs):
        start = time.time()
        result = await self._wrapped.ainvoke(input, config=config, **kwargs)
        return self._record(result, start)

    def with_structured_output(self, schema, **kwargs):
        inner = self._wrapped.with_structured_output(schema, **kwargs)
        return TrackedLLM(inner, self.provider, self.model)

    def bind(self, **kwargs):
        inner = self._wrapped.bind(**kwargs)
        return TrackedLLM(inner, self.provider, self.model)


def wrap_llm(llm: Any, provider: str, model: str) -> Any:
    """包装 LLM 实例。构造失败（罕见）→ 原样返回，绝不破坏调用链。"""
    try:
        return TrackedLLM(llm, provider, model)
    except Exception:
        return llm


def run_summary(run_id: str) -> Optional[dict]:
    """某次运行（run_id）的成本汇总：calls / total_cost_cny / total_tokens。"""
    if not run_id:
        return None
    entries = ledger.recent(run_id=run_id)
    if not entries:
        return None
    agg = ledger.aggregate_entries(entries)
    return {
        "calls": agg["calls"],
        "total_cost_cny": agg["cost_cny"],
        "total_tokens": agg["tokens"],
        "unpriced_tokens": agg["unpriced_tokens"],
    }
