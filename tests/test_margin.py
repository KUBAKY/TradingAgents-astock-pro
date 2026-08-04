"""融资余额风险线（P4）：a_stock.get_margin_data + ch0 margin_risk 信号。"""

import pandas as pd

import pytest

from tradingagents.dataflows import a_stock
from tradingagents.shortterm import ch0 as ch0_mod
from tradingagents.shortterm.ch0 import run_ch0, scan_anomalies


def _rows(rz, rq, end_date="2026-08-04"):
    return [{"SCODE": "600001", "SECURITY_NAME": "测试", "END_DATE": end_date,
             "RZYE": rz, "RQYE": rq}]


class TestGetMarginData:
    def test_parses_datacenter_row(self, monkeypatch):
        monkeypatch.setattr(a_stock, "_eastmoney_datacenter",
                            lambda *a, **k: _rows(2_000_000_000, 50_000_000))
        out = a_stock.get_margin_data("600001", "2026-08-04")
        assert out["rz_balance_yi"] == 20.0   # 20亿
        assert out["rq_balance_yi"] == 0.5    # 0.5亿
        assert out["date"] == "2026-08-04"

    def test_none_on_empty(self, monkeypatch):
        monkeypatch.setattr(a_stock, "_eastmoney_datacenter", lambda *a, **k: [])
        assert a_stock.get_margin_data("600001", "2026-08-04") is None

    def test_none_on_error(self, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("datacenter down")
        monkeypatch.setattr(a_stock, "_eastmoney_datacenter", boom)
        assert a_stock.get_margin_data("600001", "2026-08-04") is None

    def test_uses_rzrq_ggmx_report(self, monkeypatch):
        seen = {}

        def fake(report_name, **kw):
            seen["report"] = report_name
            return _rows(1e8, 1e6)
        monkeypatch.setattr(a_stock, "_eastmoney_datacenter", fake)
        a_stock.get_margin_data("600001", "2026-08-04")
        assert seen["report"] == "RPTA_RZRQ_GGMX"


def _metrics(**kw):
    base = {"ret_7d_pct": 5.0, "ret_30d_pct": 10.0, "amplitude_pct": 5.0,
            "vol_ratio_vs_5d": 1.2, "volatility_multiple": 1.0,
            "is_250d_high": False, "is_250d_low": False}
    base.update(kw)
    return base


class TestMarginRiskSignal:
    def test_margin_risk_triggered_above_15(self):
        m = _metrics(margin_to_float_mcap_pct=18.5)
        out = scan_anomalies(m, "main", "large", None,
                             {"limit_up_streak": 0, "limit_down_streak": 0})
        assert any(a["type"] == "margin_risk" and "18.5" in a["signal"] for a in out)

    def test_no_signal_below_15(self):
        m = _metrics(margin_to_float_mcap_pct=8.0)
        out = scan_anomalies(m, "main", "large", None,
                             {"limit_up_streak": 0, "limit_down_streak": 0})
        assert not any(a["type"] == "margin_risk" for a in out)

    def test_no_signal_when_absent(self):
        out = scan_anomalies(_metrics(), "main", "large", None,
                             {"limit_up_streak": 0, "limit_down_streak": 0})
        assert not any(a["type"] == "margin_risk" for a in out)


class TestRunCh0Margin:
    def _base_mocks(self, monkeypatch):
        monkeypatch.setattr(ch0_mod, "_tencent_quote",
                            lambda codes: {"600001": {"name": "测试",
                                                      "mcap_yi": 100.0,
                                                      "float_mcap_yi": 60.0,
                                                      "price": 10.0}})
        monkeypatch.setattr(ch0_mod, "count_lhb_appearances", lambda *a: 0)
        monkeypatch.setattr(ch0_mod, "_collect_auction", lambda *a, **k: None)
        monkeypatch.setattr("tradingagents.dataflows.a_stock.get_lhb_seats",
                            lambda *a, **k: None)

        n = 40
        closes = [10.0] * n
        df = pd.DataFrame({
            "Date": pd.date_range("2026-06-01", periods=n, freq="B"),
            "Open": closes, "High": [c * 1.01 for c in closes],
            "Low": [c * 0.99 for c in closes], "Close": closes,
            "Volume": [1000.0] * n,
        })
        monkeypatch.setattr(ch0_mod, "_load_ohlcv_astock", lambda *a: df)

    def test_margin_injected_into_metrics(self, monkeypatch):
        self._base_mocks(monkeypatch)
        monkeypatch.setattr(a_stock, "get_margin_data",
                            lambda *a, **k: {"date": "2026-08-04",
                                             "rz_balance_yi": 15.0,
                                             "rq_balance_yi": 0.0})
        out = run_ch0("600001", "2026-08-04")
        assert out["metrics"]["margin_balance_yi"] == 15.0
        assert out["metrics"]["margin_to_float_mcap_pct"] == 25.0  # 15/60
        assert any(a["type"] == "margin_risk" for a in out["anomalies"])
        assert "margin" not in out["data_gaps"]

    def test_margin_failure_goes_to_gaps(self, monkeypatch):
        self._base_mocks(monkeypatch)
        monkeypatch.setattr(a_stock, "get_margin_data",
                            lambda *a, **k: None)
        out = run_ch0("600001", "2026-08-04")
        assert "margin" in out["data_gaps"]
        assert out["metrics"].get("margin_to_float_mcap_pct") is None
