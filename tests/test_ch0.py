"""Ch0 纯函数单元测试（无网络依赖）。"""

import pandas as pd
import pytest

from tradingagents.shortterm.ch0 import (
    check_blacklist,
    classify_mcap,
    compute_price_metrics,
    compute_recent_bars,
    decide_mode,
    detect_board,
    detect_limit_streak,
    scan_anomalies,
)


def _df(closes, vols=None, start="2026-01-01"):
    n = len(closes)
    dates = pd.date_range(start, periods=n, freq="B")
    vols = vols or [1000.0] * n
    return pd.DataFrame({
        "Date": dates,
        "Open": closes,
        "High": [c * 1.01 for c in closes],
        "Low": [c * 0.99 for c in closes],
        "Close": closes,
        "Volume": vols,
    })


class TestDetectBoard:
    def test_main(self):
        assert detect_board("600519") == "main"
        assert detect_board("000858") == "main"

    def test_cyb_kcb_bj(self):
        assert detect_board("300750") == "cyb"
        assert detect_board("688017") == "kcb"
        assert detect_board("832000") == "bj"
        assert detect_board("430001") == "bj"

    def test_st_overrides(self):
        assert detect_board("600519", "ST茅台") == "st"
        assert detect_board("300750", "*ST宁德") == "st"
        assert detect_board("000001", "平安退") == "st"


class TestClassifyMcap:
    def test_tiers(self):
        assert classify_mcap(30) == "micro"
        assert classify_mcap(75) == "small"
        assert classify_mcap(300) == "mid"
        assert classify_mcap(2000) == "large"

    def test_st(self):
        assert classify_mcap(2000, board="st") == "st"


class TestLimitStreak:
    def test_consecutive_limit_ups_main(self):
        closes = [10.0, 11.0, 12.1, 13.31]  # 3个10%涨停
        df = _df(closes)
        assert detect_limit_streak(df, 10.0)["limit_up_streak"] == 3

    def test_broken_streak(self):
        closes = [10.0, 11.0, 11.5, 12.65]  # 中间断板
        df = _df(closes)
        assert detect_limit_streak(df, 10.0)["limit_up_streak"] == 1

    def test_limit_down(self):
        closes = [10.0, 9.0, 8.1]
        df = _df(closes)
        assert detect_limit_streak(df, 10.0)["limit_down_streak"] == 2

    def test_cyb_20pct(self):
        closes = [10.0, 12.0, 14.4]
        df = _df(closes)
        assert detect_limit_streak(df, 20.0)["limit_up_streak"] == 2


class TestPriceMetrics:
    def test_basic(self):
        closes = [10.0 + i * 0.1 for i in range(60)]
        m = compute_price_metrics(_df(closes))
        assert m["ret_7d_pct"] is not None
        assert m["ret_7d_pct"] > 0
        assert m["bars"] == 60
        assert m["is_250d_high"]  # 单调上涨必创新高

    def test_insufficient_data(self):
        m = compute_price_metrics(_df([10.0] * 10))
        assert "error" in m

    def test_vol_ratio(self):
        closes = [10.0] * 10
        vols = [100.0] * 9 + [500.0]
        m = compute_price_metrics(_df(closes * 3, vols * 3))
        assert m["vol_ratio_vs_5d"] == pytest.approx(5.0, rel=0.01)


class TestRecentBars:
    def test_length_capped_at_7(self):
        m = compute_price_metrics(_df([10.0 + i * 0.1 for i in range(60)]))
        assert len(m["recent_bars"]) == 7

    def test_short_history_uses_all(self):
        bars = compute_recent_bars(_df([10.0] * 5))
        assert len(bars) == 4  # i 从 1 起（无前收），历史不足 n 时全用

    def test_pct_chg_and_close_pos(self):
        # 单调上涨、High=Close*1.01/Low=Close*0.99 → 每日 pct>0，收位固定
        m = compute_price_metrics(_df([10.0 + i for i in range(30)]))
        last = m["recent_bars"][-1]
        assert last["pct_chg"] > 0
        # Open=Close, High=C*1.01, Low=C*0.99 → 收位=(C-0.99C)/(0.02C)=0.5
        assert last["close_pos"] == pytest.approx(0.5, abs=0.01)

    def test_close_at_high_gives_pos_1(self):
        dates = pd.date_range("2026-01-01", periods=3, freq="B")
        df = pd.DataFrame({
            "Date": dates,
            "Open": [10.0, 10.0, 10.0],
            "High": [10.0, 10.0, 11.0],
            "Low": [10.0, 10.0, 10.0],
            "Close": [10.0, 10.0, 11.0],  # 末根收在最高
            "Volume": [100.0, 100.0, 100.0],
        })
        bars = compute_recent_bars(df)
        assert bars[-1]["close_pos"] == 1.0
        assert bars[-1]["upper_shadow_pct"] == 0.0

    def test_long_upper_shadow_detected(self):
        dates = pd.date_range("2026-01-01", periods=3, freq="B")
        df = pd.DataFrame({
            "Date": dates,
            "Open": [10.0, 10.0, 10.0],
            "High": [10.0, 10.0, 11.5],   # 冲高15%
            "Low": [10.0, 10.0, 9.9],
            "Close": [10.0, 10.0, 10.0],  # 回落至开盘=炸板形态
            "Volume": [100.0, 100.0, 500.0],
        })
        bars = compute_recent_bars(df)
        assert bars[-1]["upper_shadow_pct"] == pytest.approx(15.0, rel=0.01)
        assert bars[-1]["close_pos"] < 0.1
        assert bars[-1]["vol_ratio"] == pytest.approx(5.0, rel=0.01)

    def test_metrics_include_recent_bars(self):
        m = compute_price_metrics(_df([10.0 + i * 0.05 for i in range(40)]))
        assert "recent_bars" in m
        b = m["recent_bars"][-1]
        assert set(b) == {"date", "pct_chg", "close_pos", "upper_shadow_pct", "vol_ratio"}


class TestScanAnomalies:
    def _metrics(self, **kw):
        base = {"ret_7d_pct": 5.0, "amplitude_pct": 3.0, "vol_ratio_vs_5d": 1.0,
                "volatility_multiple": 1.0, "is_250d_high": False, "is_250d_low": False}
        base.update(kw)
        return base

    def test_quiet(self):
        assert scan_anomalies(self._metrics(), "main", "mid", 3.0,
                              {"limit_up_streak": 0, "limit_down_streak": 0}) == []

    def test_main_board_threshold(self):
        a = scan_anomalies(self._metrics(ret_7d_pct=25.0), "main", "mid", 3.0,
                           {"limit_up_streak": 0, "limit_down_streak": 0})
        assert any(x["type"] == "price" for x in a)

    def test_cyb_higher_threshold_not_triggered(self):
        a = scan_anomalies(self._metrics(ret_7d_pct=25.0), "cyb", "mid", 3.0,
                           {"limit_up_streak": 0, "limit_down_streak": 0})
        assert not any(x["type"] == "price" for x in a)

    def test_micro_volume_3x(self):
        a = scan_anomalies(self._metrics(vol_ratio_vs_5d=2.5), "main", "micro", 3.0,
                           {"limit_up_streak": 0, "limit_down_streak": 0})
        assert not any(x["type"] == "volume" for x in a)
        a = scan_anomalies(self._metrics(vol_ratio_vs_5d=3.5), "main", "micro", 3.0,
                           {"limit_up_streak": 0, "limit_down_streak": 0})
        assert any(x["type"] == "volume" for x in a)

    def test_limit_up_flag(self):
        a = scan_anomalies(self._metrics(), "main", "mid", 3.0,
                           {"limit_up_streak": 2, "limit_down_streak": 0})
        assert any(x["type"] == "limit_up" for x in a)

    def test_overheat_30d(self):
        m = self._metrics()
        m["ret_30d_pct"] = 55.0
        a = scan_anomalies(m, "main", "mid", 3.0,
                           {"limit_up_streak": 0, "limit_down_streak": 0})
        assert any(x["type"] == "overheat" for x in a)

        m["ret_30d_pct"] = 45.0
        a = scan_anomalies(m, "main", "mid", 3.0,
                           {"limit_up_streak": 0, "limit_down_streak": 0})
        assert not any(x["type"] == "overheat" for x in a)


class TestBlacklist:
    def test_st_red(self):
        hits = check_blacklist("ST某某", _df([10.0] * 30), "2026-02-15")
        assert any(h["level"] == "red" and "ST" in h["rule"] for h in hits)

    def test_tui_red(self):
        hits = check_blacklist("平安退", _df([10.0] * 30), "2026-02-15")
        assert any(h["level"] == "red" for h in hits)

    def test_suspension_red(self):
        df = _df([10.0] * 30, start="2025-01-01")
        hits = check_blacklist("正常股", df, "2026-06-01")
        assert any(h["rule"].startswith("停牌") for h in hits)

    def test_new_listing_yellow(self):
        df = _df([10.0] * 100, start="2026-01-01")
        hits = check_blacklist("新股", df, "2026-06-01")
        assert any(h["level"] == "yellow" and "上市<1年" in h["rule"] for h in hits)

    def test_clean(self):
        df = _df([10.0] * 300, start="2024-06-01")
        hits = check_blacklist("贵州茅台", df, "2025-08-01")
        assert hits == []


class TestDecideMode:
    def test_limit_up_ultra_short(self):
        m = decide_mode([{"type": "limit_up", "signal": "x"}], {"ret_7d_pct": 21}, 0)
        assert m["mode"] == "ultra_short"

    def test_lhb_plus_surge(self):
        m = decide_mode([{"type": "price", "signal": "x"}], {"ret_7d_pct": 35}, 2)
        assert m["mode"] == "ultra_short"

    def test_swing_default(self):
        m = decide_mode([{"type": "volume", "signal": "x"}], {"ret_7d_pct": 5}, 0)
        assert m["mode"] == "swing"
