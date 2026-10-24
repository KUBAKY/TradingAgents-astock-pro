"""新浪财报解析：data.report_list 结构 + 分析日可见性。

v0.2.5 起三张财报表只认 data.<fzb|lrb|llb> 行列表，而接口实际返回
data.report_list{报告期: {publish_date, update_time, data: [...]}}，
于是三个取数函数一直静默返回「No ... data found」（上游 #116）。
这里用假响应把新解析与「未公布不得提前看到」两条钉住，不碰网络。
"""

import pandas as pd
import pytest

from tradingagents.dataflows import a_stock


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def _payload(report_list):
    return {"result": {"data": {"report_count": len(report_list), "report_list": report_list}}}


def _report(items, publish_date, update_time):
    return {
        "publish_date": publish_date,
        "update_time": update_time,
        "data": [
            {"item_title": title, "item_value": value, "item_display": display}
            for title, value, display in items
        ],
    }


def _install(monkeypatch, payload):
    monkeypatch.setattr(a_stock._requests, "get", lambda *a, **k: _FakeResponse(payload))


def test_balance_sheet_reads_report_list_structure(monkeypatch):
    _install(monkeypatch, _payload({
        "20240331": _report([("货币资金", "1000", ""), ("资产总计", "5000", "")], "20240429", 1714348800),
    }))

    df = a_stock._get_financial_report_sina("600519", "资产负债表", "quarterly")

    assert not df.empty
    assert "报告日" in df.columns and "货币资金" in df.columns
    assert df.iloc[0]["资产总计"] == "5000"
    # 两个日期列只在内部筛选，不得进入返回表
    assert "_公告日" not in df.columns and "_入库时间" not in df.columns


def test_reports_published_after_analysis_date_are_hidden(monkeypatch):
    _install(monkeypatch, _payload({
        "20240331": _report([("货币资金", "1100", "")], "20240429", 1714348800),
        "20231231": _report([("货币资金", "1000", "")], "20240329", 1711670400),
    }))

    df = a_stock._get_financial_report_sina("600519", "资产负债表", "quarterly", "2024-04-10")

    assert list(df["报告日"]) == [pd.Timestamp("2023-12-31")]


def test_legacy_row_list_structure_still_supported(monkeypatch):
    _install(monkeypatch, {"result": {"data": {"fzb": [{"报告日": "2023-12-31", "货币资金": "1000"}]}}})

    df = a_stock._get_financial_report_sina("600519", "资产负债表", "quarterly")

    assert not df.empty
    assert df.iloc[0]["货币资金"] == "1000"


def test_duplicate_item_titles_do_not_overwrite_each_other(monkeypatch):
    # 利润表里「营业总收入」下与「财务费用」下各有一个「利息收入」：
    # 第一个保留原名，第二个（同名已存在）记作「财务费用-利息收入」，不互相覆盖
    _install(monkeypatch, _payload({
        "20231231": _report(
            [
                ("营业总收入", "200", ""),
                ("利息收入", "30", "缩进"),
                ("财务费用", "50", ""),
                ("利息收入", "12", "缩进"),
            ],
            "20240329", 1711670400,
        ),
    }))

    df = a_stock._get_financial_report_sina("600519", "利润表", "quarterly")

    assert df.iloc[0]["营业总收入"] == "200"
    assert df.iloc[0]["利息收入"] == "30"
    assert df.iloc[0]["财务费用-利息收入"] == "12"
