"""巨潮公告分类与市场列映射测试（纯函数，不触网）。"""

from tradingagents.dataflows.cninfo import _column, classify


def test_column_mapping():
    assert _column("600519") == "sse"
    assert _column("300308") == "szse"
    assert _column("920819") == "szse"      # 北交所先用 szse 兜底


def test_classify_detects_hard_conditions():
    titles = [
        "2025年年度报告（保留意见）",
        "关于收到中国证监会立案告知书的公告",
        "关于公司股票被实施退市风险警示的公告",
        "关于2026年半年度权益分派的公告",
    ]
    out = classify(titles)
    assert "审计非标" in out
    assert "立案调查" in out
    assert "退市风险" in out
    assert "业绩预告" not in out


def test_classify_empty_when_no_hits():
    assert classify(["关于董事会换届的公告"]) == {}
