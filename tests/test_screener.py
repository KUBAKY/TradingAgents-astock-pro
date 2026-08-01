"""screener 纯逻辑单元测试（mock 网络层，无真实请求）。"""

import json

import pytest

from tradingagents.shortterm import screener


def _row(code, name="测试股", mcap_yi=100.0, amount=5.0):
    return {
        "code": code, "name": name, "price": 10.0, "chg_pct": 3.0,
        "turnover_pct": 8.0, "vol_ratio": 2.0, "amount_yi": amount,
        "mcap_yi": mcap_yi, "float_mcap_yi": mcap_yi * 0.7,
        "main_net_inflow_yi": 0.5, "main_net_inflow_pct": 1.2,
        "industry": "半导体",
    }


def _ch0_pass(r30=10.0):
    return {
        "verdict": "PASS",
        "metrics": {"ret_7d_pct": 5.0, "ret_30d_pct": r30, "last_close": 10.0},
        "anomalies": [],
        "mode_hint": {"mode": "swing", "label": "3-10日波段", "reason": "x"},
        "limit_streak": {"limit_up_streak": 0, "limit_down_streak": 0},
        "lhb_appearances_10d": 1,
        "data_gaps": [],
    }


class TestFetchBoardMerged:
    def test_dedupes_by_code_across_sorts(self, monkeypatch):
        def fake_snapshot(board, sort_key="f6", top=100):
            if sort_key == "f6":
                return [_row("600001"), _row("600002")]
            if sort_key == "f8":
                return [_row("600002"), _row("600003")]  # 600002 重复
            return [_row("600004")]
        monkeypatch.setattr(screener, "fetch_board_snapshot", fake_snapshot)

        merged = screener.fetch_board_merged("main")
        codes = [r["code"] for r in merged]
        assert sorted(codes) == ["600001", "600002", "600003", "600004"]
        assert len(codes) == len(set(codes))

    def test_covers_all_sort_keys(self, monkeypatch):
        seen = []

        def fake_snapshot(board, sort_key="f6", top=100):
            seen.append(sort_key)
            return []
        monkeypatch.setattr(screener, "fetch_board_snapshot", fake_snapshot)

        screener.fetch_board_merged("cyb")
        assert seen == list(screener.SORT_KEYS)


class TestScanRejected:
    def _run_scan(self, monkeypatch, ch0_by_code):
        monkeypatch.setattr(
            screener, "fetch_board_merged",
            lambda board: [_row(f"{board}000{i}") for i in range(len(ch0_by_code))],
        )
        monkeypatch.setattr(screener, "count_lhb_batch", lambda codes, d, days: None)
        monkeypatch.setattr(screener, "_concept_tags", lambda code: "半导体/芯片")

        def fake_ch0(code, trade_date, **kw):
            idx = int(code[-1])
            outcome = ch0_by_code[idx]
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        monkeypatch.setattr(screener, "run_ch0", fake_ch0)
        return screener.scan(capital=100000, per_board=10)

    def test_blacklist_rejected_with_reason(self, monkeypatch):
        ch0 = _ch0_pass()
        ch0["verdict"] = "BLACKLIST"
        ch0["blacklist"] = [{"level": "red", "rule": "ST股", "evidence": "名称: ST测试"}]
        result = self._run_scan(monkeypatch, [ch0])
        for board in screener.BOARD_FS:
            assert result["boards"][board] == []
            assert len(result["rejected"][board]) == 1
            assert "黑名单" in result["rejected"][board][0]["reason"]
            assert "ST股" in result["rejected"][board][0]["reason"]

    def test_downtrend_rejected(self, monkeypatch):
        result = self._run_scan(monkeypatch, [_ch0_pass(r30=-35.0)])
        for board in screener.BOARD_FS:
            assert result["boards"][board] == []
            assert "出货" in result["rejected"][board][0]["reason"]

    def test_ch0_error_rejected(self, monkeypatch):
        result = self._run_scan(monkeypatch, [RuntimeError("网络超时")])
        for board in screener.BOARD_FS:
            assert result["boards"][board] == []
            assert "ch0扫描失败" in result["rejected"][board][0]["reason"]

    def test_pass_goes_to_candidates(self, monkeypatch):
        result = self._run_scan(monkeypatch, [_ch0_pass(r30=10.0), _ch0_pass(r30=20.0)])
        for board in screener.BOARD_FS:
            assert len(result["boards"][board]) == 2
            assert result["rejected"][board] == []
            assert result["boards"][board][0]["ch0"]["mode"] == "swing"


class TestRecommendPrompt:
    def test_full_candidates_and_rejected_in_prompt(self, monkeypatch):
        captured = {}

        class _FakeLLM:
            def invoke(self, prompt):
                captured["prompt"] = prompt
                return type("R", (), {"content": "ok"})()

        monkeypatch.setattr(screener, "build_llm", lambda *a, **k: _FakeLLM())

        # 10 只候选（超过旧的 rows[:6] 截断）+ 2 只剔除
        rows = [_row(f"6000{i:02d}") for i in range(10)]
        scan_result = {
            "trade_date": "2026-08-01", "capital": 200000,
            "boards": {"main": rows, "cyb": [], "kcb": []},
            "rejected": {"main": [{"code": "600099", "name": "剔除股", "reason": "黑名单: ST股"}],
                         "cyb": [], "kcb": []},
        }
        screener.recommend(scan_result, "deepseek", "deepseek-chat")

        prompt = captured["prompt"]
        assert "600009" in prompt  # 第10只也在（无截断）
        assert "600099" in prompt and "剔除股" in prompt
        assert "200,000" in prompt
        # JSON 结构合法
        assert json.loads(prompt.split("候选池 JSON：")[1].split("已剔除列表 JSON：")[0])


class TestQuoteFromSnapshot:
    def test_maps_ch0_fields(self):
        q = screener._quote_from_snapshot(_row("600001"))
        assert q == {"name": "测试股", "price": 10.0,
                     "mcap_yi": 100.0, "float_mcap_yi": 70.0}


class TestScanConcurrency:
    def test_lhb_batch_and_concepts_wired(self, monkeypatch):
        calls = {}

        monkeypatch.setattr(screener, "fetch_board_merged",
                            lambda board: [_row(f"{board}0000")])
        monkeypatch.setattr(screener, "run_ch0",
                            lambda code, d, **kw: _ch0_pass())

        def fake_batch(codes, trade_date, days):
            calls["batch"] = codes
            return {c: 2 for c in codes}
        monkeypatch.setattr(screener, "count_lhb_batch", fake_batch)
        monkeypatch.setattr(screener, "_concept_tags",
                            lambda c: calls.setdefault("concepts", []).append(c) or "标签")

        result = screener.scan(per_board=5)
        for board in screener.BOARD_FS:
            assert calls["batch"]  # 批量 LHB 被调用
            assert result["boards"][board][0]["concepts"] == "标签"
            assert result["boards"][board][0]["ch0"]["lhb_10d"] == 1  # fake ch0 返回值

    def test_lhb_batch_failure_falls_back(self, monkeypatch):
        seen = {}

        monkeypatch.setattr(screener, "fetch_board_merged",
                            lambda board: [_row(f"{board}0000")])
        monkeypatch.setattr(screener, "count_lhb_batch", lambda *a: None)

        def fake_ch0(code, trade_date, quote=None, lhb_count=None):
            seen["lhb_count"] = lhb_count
            seen["quote"] = quote
            return _ch0_pass()
        monkeypatch.setattr(screener, "run_ch0", fake_ch0)
        monkeypatch.setattr(screener, "_concept_tags", lambda c: "")

        screener.scan(per_board=5)
        assert seen["lhb_count"] is None  # 批量失败 → 逐票回退
        assert seen["quote"]["name"] == "测试股"  # 快照 quote 注入生效
