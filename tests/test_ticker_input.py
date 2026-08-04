"""ticker_input 组件核心逻辑单测（渲染路径由 Web UI 实弹验证）。"""

from __future__ import annotations

from web.components.ticker_input import _extract_code


class TestExtractCode:
    def test_plain_name_code(self):
        assert _extract_code("京东方Ａ 000725") == "000725"

    def test_short_name(self):
        assert _extract_code("茅台 600519") == "600519"

    def test_st_stock_asterisk(self):
        assert _extract_code("*ST富吉 688401") == "688401"

    def test_trailing_spaces(self):
        assert _extract_code("中国平安 601318 ") == "601318"

    def test_code_only_option(self):
        assert _extract_code("000725") == "000725"
