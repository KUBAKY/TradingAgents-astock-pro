"""feedback.py 误差报告 + 注入块测试。conftest 已隔离 registry 目录。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tradingagents.analysis_registry import registry
from tradingagents.analysis_registry.feedback import (
    build_feedback_report,
    feedback_injection_block,
    scored_records,
)


def _seed_record(kind="stock", ticker="000725", trade_date="2026-07-01",
                 ts=100, verdict="对", direction="买入", confidence="高",
                 mode="swing", anomaly=None, t3=3.2):
    rid = registry.register(
        kind, ticker, trade_date, ts=ts,
        path=f"/x/{ticker}_{ts}.json",
        summary={
            "direction": direction, "confidence": confidence, "mode": mode,
            "anomaly_types": [anomaly] if anomaly else [],
        },
    )
    assert rid
    payload = {
        "record_id": rid, "verdict": verdict, "t3_close_pct": t3,
        "t10_close_pct": t3 * 2, "verdict_basis": "T+3 收益>0",
    }
    registry.update_record(rid, validation=payload)
    return rid


class TestScored:
    def test_filters_only_scored(self):
        _seed_record(ts=1, verdict="对")
        _seed_record(ts=2, verdict="错")
        _seed_record(ts=3, verdict="待验证")
        recs = scored_records()
        assert len(recs) == 2
        assert {r["validation"]["verdict"] for r in recs} == {"对", "错"}

    def test_empty(self):
        assert scored_records() == []


class TestAggregation:
    def test_loss_rate_by_direction(self):
        _seed_record(ts=1, verdict="对", direction="买入")
        _seed_record(ts=2, verdict="错", direction="买入")
        _seed_record(ts=3, verdict="对", direction="卖出")
        rep = build_feedback_report(asof="2026-08-04")
        assert rep["total"] == 3
        assert rep["wins"] == 2
        assert rep["win_rate"] == 66.7
        md = rep["markdown"]
        # 买入：2 样本 1 胜 → 错判率 50%
        assert "| 买入 | 2 | 1 | 1 | 50.0% |" in md
        assert "| 卖出 | 1 | 1 | 0 | 0.0% |" in md

    def test_group_by_confidence_mode_anomaly(self):
        _seed_record(ts=1, verdict="错", confidence="高", mode="swing",
                     anomaly="放量")
        _seed_record(ts=2, verdict="对", confidence="高", mode="swing",
                     anomaly="放量")
        _seed_record(ts=3, verdict="错", confidence="低", mode="ultra_short")
        rep = build_feedback_report(asof="2026-08-04")
        md = rep["markdown"]
        assert "| 高 | 2 | 1 | 1 | 50.0% |" in md
        assert "| 低 | 1 | 0 | 1 | 100.0% |" in md
        assert "| swing | 2 | 1 | 1 | 50.0% |" in md
        assert "| ultra_short | 1 | 0 | 1 | 100.0% |" in md
        assert "| 放量 | 2 | 1 | 1 | 50.0% |" in md

    def test_report_file_written(self, tmp_path):
        _seed_record(ts=1, verdict="对")
        rep = build_feedback_report(asof="2026-08-04")
        p = Path(rep["path"])
        assert p.exists()
        text = p.read_text(encoding="utf-8")
        assert "验证误差分析报告（2026-08-04）" in text
        assert "总体胜率" in text


class TestInjection:
    def test_block_with_history_and_warning(self):
        """同方向错判率≥50% → 出警示；块含该票结论。"""
        _seed_record(ts=1, verdict="错", direction="买入")
        _seed_record(ts=2, verdict="错", direction="买入")
        _seed_record(ts=3, verdict="对", direction="卖出", ticker="600519")
        blk = feedback_injection_block("000725")
        assert "该标的近期验证结论" in blk
        assert "方向=买入" in blk
        assert "⚠ 全市场同方向（买入）" in blk
        assert "100.0%" in blk

    def test_block_no_warning_when_rate_ok(self):
        _seed_record(ts=1, verdict="对", direction="买入")
        _seed_record(ts=2, verdict="对", direction="买入")
        blk = feedback_injection_block("000725")
        assert "⚠" not in blk

    def test_block_empty_without_data(self):
        assert feedback_injection_block("999999") == ""

    def test_block_other_ticker_ignores(self):
        _seed_record(ts=1, verdict="错", direction="买入", ticker="600519")
        assert feedback_injection_block("000725") == ""


class TestReportNoData:
    def test_empty_report(self, tmp_path):
        rep = build_feedback_report(asof="2026-08-04")
        assert rep["total"] == 0
        assert rep["win_rate"] is None
        assert Path(rep["path"]).exists()
