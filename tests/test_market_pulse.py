"""#1 融合：盘面信号（market_pulse）注入主线 analyst 的测试（无网络依赖）。"""

import types

import pytest
from langchain_core.runnables import RunnableLambda

import tradingagents.dataflows.a_stock as a_stock_mod
import tradingagents.shortterm.ch0 as ch0_mod
import tradingagents.shortterm.pulse as pulse_mod
import tradingagents.shortterm.screener as screener_mod
from tradingagents.agents.analysts import policy_analyst as policy_mod
from tradingagents.agents.analysts import social_media_analyst as social_mod
from tradingagents.graph.propagation import Propagator

_CH0 = {
    "ticker": "300750", "name": "宁德时代", "trade_date": "2026-08-04",
    "verdict": "PASS", "board": "cyb", "mcap_tier": "large", "mcap_yi": 8000,
    "blacklist": [], "metrics": {"margin_to_float_mcap_pct": 18.5},
    "limit_streak": {"limit_up_streak": 2, "limit_down_streak": 0},
    "turnover_pct": 12.3, "lhb_appearances_10d": 3, "anomalies": [
        {"type": "volume_breakout", "signal": "放量突破 5 日均量 2 倍"},
        {"type": "margin_risk", "signal": "融资余额/流通市值 18.5% 超警戒线"},
    ],
    "mode_hint": {"mode": "swing", "label": "波段启动", "reason": "放量突破+融资抬升"},
    "auction": None, "institutional_flow": {"buy_yi": 2.0, "sell_yi": 0.5, "net_yi": 1.5},
    "data_gaps": [],
}

_SENTI = {"sentiment": "ok", "limit_up_count": 138, "max_streak": 7,
          "limit_down_count": 3, "label": "高潮", "score": 90}


@pytest.fixture(autouse=True)
def _mock_data(monkeypatch):
    monkeypatch.setattr(a_stock_mod, "resolve_ticker", lambda s: "300750")
    monkeypatch.setattr(ch0_mod, "run_ch0", lambda *a, **k: dict(_CH0))
    monkeypatch.setattr(screener_mod, "fetch_market_sentiment", lambda *a, **k: dict(_SENTI))


class TestBuildMarketPulse:
    def test_full_pulse_text(self):
        text = pulse_mod.build_market_pulse("300750", "2026-08-04")
        assert "短线异动精扫" in text
        assert "宁德时代" in text
        assert "波段启动" in text
        assert "放量突破" in text
        assert "margin_risk" not in text  # 显示中文标签而非 type
        assert "融资余额/流通市值: 18.5%" in text
        assert "机构动向" in text and "净 1.5 亿" in text
        assert "情绪温度计: 高潮" in text
        assert "涨停 138 家" in text and "7 板" in text

    def test_blacklist_short_text(self, monkeypatch):
        bl = dict(_CH0)
        bl["verdict"] = "BLACKLIST"
        bl["blacklist"] = [{"level": "red", "rule": "ST股", "evidence": "名称: *ST富吉"}]
        monkeypatch.setattr(ch0_mod, "run_ch0", lambda *a, **k: bl)
        text = pulse_mod.build_market_pulse("300750", "2026-08-04")
        assert "黑名单" in text and "ST股" in text

    def test_ch0_failure_degrades_to_sentiment_only(self, monkeypatch):
        monkeypatch.setattr(ch0_mod, "run_ch0", lambda *a, **k: (_ for _ in ()).throw(ValueError("boom")))
        text = pulse_mod.build_market_pulse("300750", "2026-08-04")
        assert "情绪温度计: 高潮" in text
        assert "短线异动精扫" not in text

    def test_all_failure_returns_empty(self, monkeypatch):
        monkeypatch.setattr(ch0_mod, "run_ch0", lambda *a, **k: (_ for _ in ()).throw(ValueError("boom")))
        monkeypatch.setattr(screener_mod, "fetch_market_sentiment", lambda *a, **k: (_ for _ in ()).throw(ValueError("boom")))
        assert pulse_mod.build_market_pulse("300750", "2026-08-04") == ""

    def test_resolve_failure_returns_empty(self, monkeypatch):
        monkeypatch.setattr(a_stock_mod, "resolve_ticker", lambda s: (_ for _ in ()).throw(ValueError("no such")))

        def _bad_to_code(t):
            raise ValueError("no such")
        monkeypatch.setattr(pulse_mod, "_to_code", _bad_to_code)
        assert pulse_mod.build_market_pulse("找不到的股票", "2026-08-04") == ""

    def test_max_chars_truncation(self):
        text = pulse_mod.build_market_pulse("300750", "2026-08-04", max_chars=120)
        assert len(text) == 128  # 120 + "\n...[截断]"
        assert text.endswith("...[截断]")

    def test_sentiment_unknown_omitted(self, monkeypatch):
        monkeypatch.setattr(screener_mod, "fetch_market_sentiment",
                            lambda *a, **k: {"sentiment": "unknown"})
        text = pulse_mod.build_market_pulse("300750", "2026-08-04")
        assert "情绪温度计" not in text
        assert "短线异动精扫" in text


class TestPropagationInject:
    def test_state_contains_market_pulse(self, monkeypatch):
        pulse_mod.build_market_pulse = lambda *a, **k: "PULSE-TEXT"
        state = Propagator().create_initial_state("300750", "2026-08-04")
        assert state["market_pulse"] == "PULSE-TEXT"

    def test_returns_empty_on_build_failure(self, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("boom")
        monkeypatch.setattr(pulse_mod, "build_market_pulse", boom)
        state = Propagator().create_initial_state("300750", "2026-08-04")
        assert state["market_pulse"] == ""


class _FakeResult:
    content = "分析报告内容"
    tool_calls = []


class _FakeChain:
    def __init__(self, captured):
        self.captured = captured

    def __ror__(self, other):
        def _invoke(x):
            return _FakeResult()
        self.captured["chain"] = other
        return other | RunnableLambda(_invoke)


class _SpyTemplate:
    """记录 partial 调用的模板替身（用于捕获渲染后的变量）。"""

    def __init__(self, captured):
        self.captured = captured
        self._real = None

    def partial(self, **kwargs):
        self.captured.setdefault("partials", []).append(kwargs)
        return self

    def __or__(self, other):
        def _invoke(x):
            return _FakeResult()
        return RunnableLambda(_invoke)


def _make_spy(monkeypatch, mod):
    captured: dict = {}

    class SpyChatPromptTemplate:
        @classmethod
        def from_messages(cls, messages):
            captured["template"] = messages
            return _SpyTemplate(captured)

    monkeypatch.setattr(mod, "ChatPromptTemplate", SpyChatPromptTemplate)
    return captured


def _node_state(pulse_text: str):
    return {
        "trade_date": "2026-08-04",
        "company_of_interest": "300750",
        "market_pulse": pulse_text,
        "messages": [("human", "300750")],
    }


class _FakeLLM:
    """node 内 `llm.bind_tools(tools)` 的替身；_SpyTemplate.__or__ 接管后续。"""

    def bind_tools(self, tools):
        return self


def _merged_partials(captured) -> dict:
    merged: dict = {}
    for p in captured.get("partials", []):
        merged.update(p)
    return merged


class TestSocialAnalystPulse:
    def test_pulse_injected_into_system_message(self, monkeypatch):
        captured = _make_spy(monkeypatch, social_mod)
        node = social_mod.create_social_media_analyst(llm=_FakeLLM())
        node(_node_state("PULSE-TEXT"))
        partials = _merged_partials(captured)
        assert partials.get("market_pulse") == "PULSE-TEXT"
        assert "短线盘面信号参考" in partials.get("system_message", "")

    def test_empty_pulse_tolerated(self, monkeypatch):
        captured = _make_spy(monkeypatch, social_mod)
        node = social_mod.create_social_media_analyst(llm=_FakeLLM())
        node(_node_state(""))
        assert _merged_partials(captured).get("market_pulse") == ""


class TestPolicyAnalystPulse:
    def test_pulse_injected_with_cross_check(self, monkeypatch):
        captured = _make_spy(monkeypatch, policy_mod)
        node = policy_mod.create_policy_analyst(llm=_FakeLLM())
        node(_node_state("PULSE-TEXT"))
        partials = _merged_partials(captured)
        assert partials.get("market_pulse") == "PULSE-TEXT"
        assert "短线盘面信号参考" in partials.get("system_message", "")
        assert "交叉验证" in partials.get("system_message", "")
