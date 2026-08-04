"""执行轨迹落盘单测（默认关闭，按需开启，90 天自动清理）。"""

import json
import os
import time

from tradingagents.shortterm import pipeline, trace


class TestTraceModule:
    def test_save_and_load(self, tmp_path, monkeypatch):
        monkeypatch.setattr(trace, "_DIR", tmp_path)
        record = {"ticker": "000725", "trade_date": "2026-07-10",
                  "mode": "swing", "prompt": "P", "response": "R",
                  "validation": {"ok": True}, "elapsed_ms": 123}
        path = trace.save_trace(record)
        assert path.exists()
        loaded = json.loads(path.read_text(encoding="utf-8"))
        assert loaded["prompt"] == "P"
        assert loaded["ts"] > 0

    def test_cleanup_removes_old(self, tmp_path, monkeypatch):
        monkeypatch.setattr(trace, "_DIR", tmp_path)
        old = tmp_path / f"old_{int(time.time()) - 100 * 86400}.json"
        old.write_text("{}", encoding="utf-8")
        os.utime(old, (time.time() - 100 * 86400, time.time() - 100 * 86400))
        trace.save_trace({"ticker": "000725", "trade_date": "2026-07-10"})
        assert not old.exists()

    def test_cleanup_keeps_recent(self, tmp_path, monkeypatch):
        monkeypatch.setattr(trace, "_DIR", tmp_path)
        recent = tmp_path / f"recent_{int(time.time())}.json"
        recent.write_text("{}", encoding="utf-8")
        trace.save_trace({"ticker": "000725", "trade_date": "2026-07-10"})
        assert recent.exists()


def _ch0_pass():
    return {
        "ticker": "000725", "name": "京东方A", "trade_date": "2026-07-10",
        "verdict": "PASS", "board": "main", "mcap_tier": "mid", "mcap_yi": 300.0,
        "blacklist": [],
        "metrics": {"last_close": 10.0, "ret_7d_pct": 5.0, "ret_30d_pct": 10.0,
                    "vol_ratio_vs_5d": 1.5, "amplitude_pct": 3.0,
                    "volatility_multiple": 1.0, "is_250d_high": False,
                    "is_250d_low": False, "recent_bars": []},
        "limit_streak": {"limit_up_streak": 0, "limit_down_streak": 0},
        "turnover_pct": 5.0, "lhb_appearances_10d": 0,
        "anomalies": [],
        "mode_hint": {"mode": "swing", "label": "3-10日波段", "reason": "x"},
        "data_gaps": [],
    }


VALID_CARD = """**方向**: 买入
**置信度**: 中
**适用周期**: 3-10日波段

### 逻辑链
1. 表面原因：放量突破
2. 深层逻辑：资金驱动
3. 时间窗口：催化初期
4. 反向思考：缩量则失效

### 交易计划
- 介入条件: 回踩10日线企稳
- 目标价: 12.0 / 13.5
- 预期周期: 5个交易日
- 仓位建议: 不超过20%
- 止损位: 9.5
- 失效条件: 跌破20日线
- 次日观察点: 竞价量能

### 机构行为反向推理
未见明显出货信号。
"""


def _run(monkeypatch, tmp_path, outputs, trace_flag=False, env=None):
    monkeypatch.setattr(trace, "_DIR", tmp_path)
    monkeypatch.setattr(pipeline, "run_ch0", lambda t, d: _ch0_pass())
    monkeypatch.setattr(pipeline, "gather_data_bundle", lambda *a: "BUNDLE")
    monkeypatch.setattr(pipeline, "load_past_evaluations", lambda t, d: [])
    if env:
        monkeypatch.setenv("ST_TRACE", env)
    else:
        monkeypatch.delenv("ST_TRACE", raising=False)
    calls = {"n": 0}

    class _FakeLLM:
        def invoke(self, prompt):
            out = outputs[min(calls["n"], len(outputs) - 1)]
            calls["n"] += 1
            return type("R", (), {"content": out})()

    monkeypatch.setattr(pipeline, "build_llm", lambda *a, **k: _FakeLLM())
    return pipeline.run("000725", "2026-07-10", trace=trace_flag), tmp_path


class TestPipelineTrace:
    def test_default_off(self, monkeypatch, tmp_path):
        result, d = _run(monkeypatch, tmp_path, [VALID_CARD])
        assert list(d.glob("*.json")) == []
        assert result.get("trace_path") is None

    def test_trace_flag_writes_file(self, monkeypatch, tmp_path):
        result, d = _run(monkeypatch, tmp_path, [VALID_CARD], trace_flag=True)
        files = list(d.glob("*.json"))
        assert len(files) == 1
        rec = json.loads(files[0].read_text(encoding="utf-8"))
        assert rec["ticker"] == "000725"
        assert "BUNDLE" in rec["prompt"]
        assert rec["response"] == VALID_CARD
        assert rec["validation"]["ok"] is True
        assert rec["attempts"] == 1
        assert result["trace_path"] == str(files[0])

    def test_env_var_enables(self, monkeypatch, tmp_path):
        result, d = _run(monkeypatch, tmp_path, [VALID_CARD], env="1")
        assert len(list(d.glob("*.json"))) == 1
        assert result["trace_path"]

    def test_retry_records_both_attempts(self, monkeypatch, tmp_path):
        bad = "**方向**: 观望\n**置信度**: 低"
        result, d = _run(monkeypatch, tmp_path, [bad, VALID_CARD], trace_flag=True)
        files = list(d.glob("*.json"))
        assert len(files) == 1
        rec = json.loads(files[0].read_text(encoding="utf-8"))
        assert rec["attempts"] == 2
        assert rec["first_response"] == bad
        assert rec["first_violations"]
        assert rec["response"] == VALID_CARD

    def test_trace_failure_non_blocking(self, monkeypatch, tmp_path):
        """落盘失败不影响主流程。"""
        monkeypatch.setattr(trace, "save_trace",
                            lambda r: (_ for _ in ()).throw(OSError("disk full")))
        result, _ = _run(monkeypatch, tmp_path, [VALID_CARD], trace_flag=True)
        assert result["report"] == VALID_CARD
        assert result.get("trace_path") is None


class TestTraceListing:
    def test_list_and_load(self, tmp_path, monkeypatch):
        monkeypatch.setattr(trace, "_DIR", tmp_path)
        trace.save_trace({"ticker": "000725", "trade_date": "2026-07-10",
                          "mode": "swing", "attempts": 1, "elapsed_ms": 100})
        trace.save_trace({"ticker": "300750", "trade_date": "2026-07-11",
                          "mode": "ultra_short", "attempts": 2, "elapsed_ms": 200})
        items = trace.list_traces()
        assert len(items) == 2
        # 新→旧排序
        assert items[0]["ticker"] == "300750"
        assert items[0]["attempts"] == 2
        assert "prompt" not in items[0]  # 摘要不含大字段
        full = trace.load_trace(items[0]["path"])
        assert full["ticker"] == "300750"

    def test_list_empty_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr(trace, "_DIR", tmp_path / "nonexist")
        assert trace.list_traces() == []

    def test_list_skips_corrupt(self, tmp_path, monkeypatch):
        monkeypatch.setattr(trace, "_DIR", tmp_path)
        tmp_path.mkdir(exist_ok=True)
        (tmp_path / "bad_1.json").write_text("{broken", encoding="utf-8")
        trace.save_trace({"ticker": "000725", "trade_date": "2026-07-10"})
        items = trace.list_traces()
        assert len(items) == 1
