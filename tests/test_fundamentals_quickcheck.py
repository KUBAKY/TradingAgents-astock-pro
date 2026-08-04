"""#3 融合：决策卡基本面快查（gather_data_bundle section + 模板雷区指令）。"""

import pytest

from tradingagents.shortterm import prompts as prompts_mod
from tradingagents.shortterm.pipeline import gather_data_bundle

_DATA_FUNCS = [
    "get_stock_data", "get_indicators", "get_fund_flow",
    "get_dragon_tiger_board", "get_concept_blocks", "get_news",
    "get_lockup_expiry", "get_insider_transactions", "get_northbound_flow",
    "get_hot_stocks", "get_fundamentals",
]


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    import tradingagents.dataflows.a_stock as a_stock_mod
    for fn in _DATA_FUNCS:
        monkeypatch.setattr(a_stock_mod, fn, lambda *a, **k: "")



class TestBundleFundamentals:
    def test_fundamentals_section_included(self, monkeypatch):
        seen = {}
        def fake_fundamentals(ticker, trade_date):
            seen["args"] = (ticker, trade_date)
            return "PE: 25.3\nPB: 3.1\nROE: 18.5%\n商誉: 12.3亿"
        monkeypatch.setattr(
            "tradingagents.dataflows.a_stock.get_fundamentals",
            fake_fundamentals,
        )
        bundle = gather_data_bundle("300750", "2026-08-04", "swing")
        assert seen["args"] == ("300750", "2026-08-04")
        assert "### 基本面快查" in bundle
        assert "PE: 25.3" in bundle

    def test_fundamentals_failure_section_still_present(self, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("network down")
        monkeypatch.setattr(
            "tradingagents.dataflows.a_stock.get_fundamentals", boom)
        bundle = gather_data_bundle("300750", "2026-08-04", "swing")
        assert "### 基本面快查" in bundle
        assert "数据获取失败" in bundle

    def test_empty_fundamentals_omitted(self, monkeypatch):
        monkeypatch.setattr(
            "tradingagents.dataflows.a_stock.get_fundamentals",
            lambda *a, **k: "  ",
        )
        bundle = gather_data_bundle("300750", "2026-08-04", "swing")
        assert "### 基本面快查" not in bundle


class TestPromptMinefield:
    def test_ultra_short_has_minefield_discipline(self):
        assert "基本面雷区检查" in prompts_mod.ULTRA_SHORT_PROMPT
        assert "踩雷跌停连跑的机会都没有" in prompts_mod.ULTRA_SHORT_PROMPT

    def test_swing_has_minefield_discipline(self):
        assert "基本面雷区检查" in prompts_mod.SWING_PROMPT
        assert "解禁/减持/业绩变脸" in prompts_mod.SWING_PROMPT

    def test_decision_card_has_minefield_field(self):
        assert "### 基本面雷区检查" in prompts_mod.DECISION_CARD_FORMAT
        assert "一票否决" in prompts_mod.DECISION_CARD_FORMAT

    def test_bundle_placeholder_present_in_both(self):
        assert "{data_bundle}" in prompts_mod.ULTRA_SHORT_PROMPT
        assert "{data_bundle}" in prompts_mod.SWING_PROMPT
