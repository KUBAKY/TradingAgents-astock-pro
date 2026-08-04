"""龙虎榜散户席位识别（P6）：a_stock.get_lhb_seats + ch0 散户接盘信号。"""

import pandas as pd

import pytest

from tradingagents.dataflows import a_stock
from tradingagents.shortterm import ch0 as ch0_mod
from tradingagents.shortterm.ch0 import run_ch0


def _seat_row(name, amt, code="0"):
    return {"OPERATEDEPT_NAME": name, "BUY": amt, "SELL": amt,
            "OPERATEDEPT_CODE": code}


class TestGetLhbSeats:
    def test_structured_buy_sell(self, monkeypatch):
        calls = []

        def fake(report, **kw):
            calls.append(report)
            if report == "RPT_DAILYBILLBOARD_DETAILSNEW":
                return [{"TRADE_DATE": "2026-08-03"}]
            if report == "RPT_BILLBOARD_DAILYDETAILSBUY":
                return [_seat_row("东方财富证券拉萨团结路第一", 5e7),
                        _seat_row("机构专用", 2e7, code="0")]
            return [_seat_row("东方财富证券拉萨东环路", 1e7),
                    _seat_row("华泰证券深圳益田路", 3e7)]

        monkeypatch.setattr(a_stock, "_eastmoney_datacenter", fake)
        out = a_stock.get_lhb_seats("600001", "2026-08-04")
        assert out["trade_date"] == "2026-08-03"
        assert out["buy"][0]["name"] == "东方财富证券拉萨团结路第一"
        assert out["buy"][0]["amount_yi"] == 0.5
        assert out["sell"][1]["amount_yi"] == 0.3
        assert calls.count("RPT_BILLBOARD_DAILYDETAILSBUY") == 1
        assert calls.count("RPT_BILLBOARD_DAILYDETAILSSELL") == 1

    def test_none_on_failure(self, monkeypatch):
        monkeypatch.setattr(a_stock, "_eastmoney_datacenter",
                            lambda *a, **k: [])
        assert a_stock.get_lhb_seats("600001", "2026-08-04") is None

    def test_no_trade_date_takes_latest(self, monkeypatch):
        filters = []

        def fake(report, **kw):
            filters.append(kw.get("filter_str", ""))
            if report == "RPT_DAILYBILLBOARD_DETAILSNEW":
                return [{"TRADE_DATE": "2026-08-03"}]
            if report == "RPT_BILLBOARD_DAILYDETAILSBUY":
                return [_seat_row("机构专用", 2e7, code="0")]
            return []

        monkeypatch.setattr(a_stock, "_eastmoney_datacenter", fake)
        out = a_stock.get_lhb_seats("600001", None)
        assert out["trade_date"] == "2026-08-03"
        assert out["buy"][0]["name"] == "机构专用"
        assert filters[0] == ""
        assert "2026-08-03" in filters[1]


class TestRetailSeatDetect:
    def test_lhasa_is_retail(self):
        assert ch0_mod._is_retail_seat("东方财富证券拉萨团结路第一")
        assert ch0_mod._is_retail_seat("东方财富证券拉萨东环路")
        assert not ch0_mod._is_retail_seat("机构专用")
        assert not ch0_mod._is_retail_seat("华泰证券深圳益田路")

    def test_takeover_signal_when_retail_buys_and_others_sell(self):
        seats = {
            "trade_date": "2026-08-03",
            "buy": [{"name": "东方财富证券拉萨团结路第一", "amount_yi": 1.0},
                    {"name": "机构专用", "amount_yi": 0.1}],
            "sell": [{"name": "东方财富证券拉萨东环路", "amount_yi": 0.2},
                     {"name": "华泰证券深圳益田路", "amount_yi": 0.8}],
        }
        out = ch0_mod.detect_retail_takeover(seats)
        assert len(out) == 1
        assert out[0]["type"] == "retail_takeover"
        assert "1.0" in out[0]["signal"]

    def test_no_signal_when_retail_not_buying(self):
        seats = {"buy": [{"name": "机构专用", "amount_yi": 1.0}],
                 "sell": [{"name": "东方财富证券拉萨东环路", "amount_yi": 0.5}]}
        assert ch0_mod.detect_retail_takeover(seats) == []

    def test_no_signal_when_sell_side_all_retail(self):
        seats = {"buy": [{"name": "东方财富证券拉萨团结路第一", "amount_yi": 1.0}],
                 "sell": [{"name": "东方财富证券拉萨东环路", "amount_yi": 1.2}]}
        assert ch0_mod.detect_retail_takeover(seats) == []


class TestRunCh0Seats:
    def _base_mocks(self, monkeypatch):
        monkeypatch.setattr(ch0_mod, "_tencent_quote",
                            lambda codes: {"600001": {"name": "测试",
                                                      "mcap_yi": 100.0,
                                                      "float_mcap_yi": 60.0,
                                                      "price": 10.0}})
        monkeypatch.setattr(ch0_mod, "count_lhb_appearances", lambda *a: 3)
        monkeypatch.setattr(ch0_mod, "_collect_auction", lambda *a, **k: None)
        monkeypatch.setattr(a_stock, "get_margin_data", lambda *a, **k: None)

        n = 40
        closes = [10.0] * n
        df = pd.DataFrame({
            "Date": pd.date_range("2026-06-01", periods=n, freq="B"),
            "Open": closes, "High": [c * 1.01 for c in closes],
            "Low": [c * 0.99 for c in closes], "Close": closes,
            "Volume": [1000.0] * n,
        })
        monkeypatch.setattr(ch0_mod, "_load_ohlcv_astock", lambda *a: df)

    def test_retail_takeover_in_anomalies(self, monkeypatch):
        self._base_mocks(monkeypatch)
        monkeypatch.setattr(a_stock, "get_lhb_seats", lambda *a, **k: {
            "trade_date": "2026-08-03",
            "buy": [{"name": "东方财富证券拉萨团结路第一", "amount_yi": 1.0}],
            "sell": [{"name": "华泰证券深圳益田路", "amount_yi": 0.8}],
        })
        out = run_ch0("600001", "2026-08-04")
        assert any(a["type"] == "retail_takeover" for a in out["anomalies"])

    def test_no_seats_silently_skipped(self, monkeypatch):
        self._base_mocks(monkeypatch)
        monkeypatch.setattr(a_stock, "get_lhb_seats", lambda *a, **k: None)
        out = run_ch0("600001", "2026-08-04")
        assert not any(a["type"] == "retail_takeover" for a in out["anomalies"])


class TestInstitutionalActivity:
    def test_parses_inst_seats(self):
        seats = {
            "buy": [{"name": "机构专用", "amount_yi": 1.5, "code": "0"},
                    {"name": "东方财富证券拉萨团结路第一", "amount_yi": 0.5, "code": "1"}],
            "sell": [{"name": "机构专用", "amount_yi": 0.3, "code": "0"}],
        }
        out = ch0_mod._institutional_activity(seats)
        assert out == {"buy_yi": 1.5, "sell_yi": 0.3, "net_yi": 1.2}

    def test_none_when_no_institutional(self):
        seats = {"buy": [{"name": "东方财富证券拉萨团结路第一", "amount_yi": 1.0, "code": "1"}],
                 "sell": [{"name": "华泰证券深圳益田路", "amount_yi": 0.5, "code": "1"}]}
        assert ch0_mod._institutional_activity(seats) is None
        assert ch0_mod._institutional_activity(None) is None

    def test_mounted_in_run_ch0(self, monkeypatch):
        monkeypatch.setattr(ch0_mod, "_tencent_quote",
                            lambda codes: {"600001": {"name": "测试",
                                                      "mcap_yi": 100.0,
                                                      "float_mcap_yi": 60.0,
                                                      "price": 10.0}})
        monkeypatch.setattr(ch0_mod, "count_lhb_appearances", lambda *a: 2)
        monkeypatch.setattr(ch0_mod, "_collect_auction", lambda *a, **k: None)
        monkeypatch.setattr("tradingagents.dataflows.a_stock.get_margin_data",
                            lambda *a, **k: None)
        monkeypatch.setattr("tradingagents.dataflows.a_stock.get_lhb_seats",
                            lambda *a, **k: {
                                "buy": [{"name": "机构专用", "amount_yi": 1.0, "code": "0"}],
                                "sell": [{"name": "机构专用", "amount_yi": 0.2, "code": "0"}]})
        n = 40
        closes = [10.0] * n
        df = pd.DataFrame({
            "Date": pd.date_range("2026-06-01", periods=n, freq="B"),
            "Open": closes, "High": [c * 1.01 for c in closes],
            "Low": [c * 0.99 for c in closes], "Close": closes,
            "Volume": [1000.0] * n,
        })
        monkeypatch.setattr(ch0_mod, "_load_ohlcv_astock", lambda *a: df)

        out = run_ch0("600001", "2026-08-04")
        assert out["institutional_flow"] == {"buy_yi": 1.0, "sell_yi": 0.2, "net_yi": 0.8}


class TestCh0SummaryBlockInstitution:
    def test_render_contains_reverse_inference(self):
        from tradingagents.shortterm.prompts import ch0_summary_block
        ch0 = {
            "name": "测试", "ticker": "600001", "board": "main",
            "mcap_tier": "large", "mcap_yi": 100.0, "turnover_pct": 5.0,
            "metrics": {"last_close": 10.0, "ret_7d_pct": 3.0, "ret_30d_pct": 10.0,
                        "vol_ratio_vs_5d": 1.5, "amplitude_pct": 4.0,
                        "volatility_multiple": 1.2, "is_250d_high": False,
                        "is_250d_low": False, "recent_bars": []},
            "limit_streak": {"limit_up_streak": 0, "limit_down_streak": 0},
            "lhb_appearances_10d": 2, "anomalies": [], "blacklist": [],
            "data_gaps": [],
            "mode_hint": {"mode": "swing", "label": "3-10日波段", "reason": "x"},
            "institutional_flow": {"buy_yi": 1.0, "sell_yi": 0.2, "net_yi": 0.8},
        }
        text = ch0_summary_block(ch0)
        assert "机构动向" in text
        assert "建仓中" in text and "出货中" in text
