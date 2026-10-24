"""行业中位数聚合的纯函数测试（不触网、不依赖 baostock）。"""

import pytest

from tradingagents.dataflows.industry import _bs_code, aggregate


def _rows(n, roe=0.1):
    return [{"symbol": f"{600000+i}", "roeAvg": roe + i * 0.01,
             "gpMargin": 0.3, "npMargin": 0.1, "YOYNI": 0.2,
             "liabilityToAsset": 0.5} for i in range(n)]


def test_bs_code_prefix_mapping():
    assert _bs_code("600519") == "sh.600519"
    assert _bs_code("000001") == "sz.000001"
    assert _bs_code("300308") == "sz.300308"
    assert _bs_code("920819") == "bj.920819"
    assert _bs_code("832000") == "bj.832000"


def test_aggregate_median_and_quantiles():
    rows = _rows(11, roe=0.10)          # roe 0.10..0.20
    out = aggregate(rows, "2026Q2", "测试行业")

    assert out["n"] == 11
    assert out["industry"] == "测试行业"
    assert out["metrics"]["roeAvg"]["median"] == pytest.approx(0.15, abs=1e-9)
    assert out["metrics"]["roeAvg"]["p25"] < out["metrics"]["roeAvg"]["median"]
    assert out["metrics"]["roeAvg"]["n"] == 11


def test_aggregate_warns_on_small_sample():
    out = aggregate(_rows(6), "2026Q2", "小样本")
    assert "warning" in out


def test_aggregate_skips_metric_with_too_few_values():
    rows = _rows(11)
    for r in rows[:7]:
        r["liabilityToAsset"] = None      # 只剩 4 个有效值
    out = aggregate(rows, "2026Q2", "缺值行业")
    assert "liabilityToAsset" not in out["metrics"]
    assert "roeAvg" in out["metrics"]


def test_aggregate_empty_rows():
    out = aggregate([], "2026Q2", "空")
    assert out["n"] == 0
    assert out["metrics"] == {}
