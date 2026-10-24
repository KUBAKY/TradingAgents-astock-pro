"""体系分析（kind=system）的登记与前向验证测试。

覆盖：登记落盘 + 索引、评级映射、10 交易日窗口、验证分派。
"""

import json

import pandas as pd
import pytest

from tradingagents.analysis_registry.validate import plan_validation_window, validate_record


def _df(after):
    rows = [("2026-10-23", 600.0, 600.0)] + after
    return pd.DataFrame({
        "Date": pd.to_datetime([r[0] for r in rows]),
        "Open": [r[1] for r in rows],
        "Close": [r[2] for r in rows],
        "High": [max(r[1], r[2]) for r in rows],
        "Low": [min(r[1], r[2]) for r in rows],
        "Volume": [1e6] * len(rows),
    })


def _mock_ohlcv(monkeypatch, df):
    import tradingagents.dataflows.a_stock as a
    monkeypatch.setattr(a, "_load_ohlcv_astock", lambda code, asof: df)


def test_register_writes_file_and_index(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADINGAGENTS_REGISTRY_DIR", str(tmp_path / "reg"))
    monkeypatch.setenv("TRADINGAGENTS_SYSTEM_DIR", str(tmp_path / "sys"))
    from scripts.register_system_analysis import register_one

    rid = register_one("300308", "2026-10-23", "观望", paradigm="价值",
                       score=55.6, confidence="低", note="PB 78% 分位")
    assert rid and rid.startswith("system:300308:2026-10-23:")

    files = list((tmp_path / "sys").glob("300308_*.json"))
    assert len(files) == 1
    payload = json.loads(files[0].read_text(encoding="utf-8"))
    assert payload["parsed"]["direction"] == "观望"
    assert payload["parsed"]["paradigm"] == "价值"

    idx = json.loads((tmp_path / "reg" / "index.json").read_text(encoding="utf-8"))
    recs = [r for r in idx["records"] if r["kind"] == "system"]
    assert len(recs) == 1 and recs[0]["summary"]["rating"] == "Hold"


def test_invalid_rating_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADINGAGENTS_REGISTRY_DIR", str(tmp_path / "reg"))
    monkeypatch.setenv("TRADINGAGENTS_SYSTEM_DIR", str(tmp_path / "sys"))
    from scripts.register_system_analysis import register_one

    with pytest.raises(ValueError):
        register_one("300308", "2026-10-23", "强烈推荐")


def test_system_uses_10_day_window():
    assert plan_validation_window({"kind": "system"}) == 10


def test_buy_record_validated_by_rise(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADINGAGENTS_REGISTRY_DIR", str(tmp_path / "reg"))
    monkeypatch.setenv("TRADINGAGENTS_SYSTEM_DIR", str(tmp_path / "sys"))
    from scripts.register_system_analysis import register_one

    rid = register_one("300308", "2026-10-23", "买入", paradigm="成长", score=72)
    idx = json.loads((tmp_path / "reg" / "index.json").read_text(encoding="utf-8"))
    rec = next(r for r in idx["records"] if r["id"] == rid)

    rise = [(f"2026-11-{d:02d}", 610.0, 610.0 + i * 10) for i, d in enumerate(range(2, 14), 1)]
    _mock_ohlcv(monkeypatch, _df(rise))

    out = validate_record(rec, asof="2026-11-13")
    assert out["kind"] == "system"
    assert out["verdict"] != "跳过"


# ---------------------------------------------------------------------------
# 观望的相对验证（AS-C8-07）
# ---------------------------------------------------------------------------

def _hold_rec(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADINGAGENTS_REGISTRY_DIR", str(tmp_path / "reg"))
    monkeypatch.setenv("TRADINGAGENTS_SYSTEM_DIR", str(tmp_path / "sys"))
    from scripts.register_system_analysis import register_one
    rid = register_one("300308", "2026-10-23", "观望", paradigm="价值", score=55.6)
    idx = json.loads((tmp_path / "reg" / "index.json").read_text(encoding="utf-8"))
    return next(r for r in idx["records"] if r["id"] == rid)


def _flat_days(pct=0.0, n=10):
    rows = []
    for i in range(1, n + 1):
        px = 600.0 * (1 + pct * i)
        rows.append((f"2026-11-{i+1:02d}", px, px))
    return rows


def test_hold_missed_when_stock_outperforms(tmp_path, monkeypatch):
    from tradingagents.analysis_registry import validate as vmod
    rec = _hold_rec(tmp_path, monkeypatch)
    _mock_ohlcv(monkeypatch, _df(_flat_days(0.02)))          # 股票 +20%
    monkeypatch.setattr(vmod, "_bench_window_return", lambda a, b: 0.01)  # 基准 +10%

    out = validate_record(rec, asof="2026-11-13")
    assert out["verdict"] == "观望错过"
    assert "跑赢基准" in out["verdict_basis"]


def test_hold_correct_when_stock_underperforms(tmp_path, monkeypatch):
    from tradingagents.analysis_registry import validate as vmod
    rec = _hold_rec(tmp_path, monkeypatch)
    _mock_ohlcv(monkeypatch, _df(_flat_days(0.0)))            # 股票持平
    monkeypatch.setattr(vmod, "_bench_window_return", lambda a, b: 0.10)  # 基准 +10%

    out = validate_record(rec, asof="2026-11-13")
    assert out["verdict"] == "观望正确"


def test_hold_neutral_within_band(tmp_path, monkeypatch):
    from tradingagents.analysis_registry import validate as vmod
    rec = _hold_rec(tmp_path, monkeypatch)
    _mock_ohlcv(monkeypatch, _df(_flat_days(0.01)))           # 股票 +10%
    monkeypatch.setattr(vmod, "_bench_window_return", lambda a, b: 0.09)  # 超额 +1%

    out = validate_record(rec, asof="2026-11-13")
    assert out["verdict"] == "观望成立"


def test_hold_pending_when_benchmark_missing(tmp_path, monkeypatch):
    from tradingagents.analysis_registry import validate as vmod
    rec = _hold_rec(tmp_path, monkeypatch)
    _mock_ohlcv(monkeypatch, _df(_flat_days(0.01)))
    monkeypatch.setattr(vmod, "_bench_window_return", lambda a, b: None)

    out = validate_record(rec, asof="2026-11-13")
    assert out["verdict"] == "待验证"
    assert "基准" in out["verdict_basis"]
