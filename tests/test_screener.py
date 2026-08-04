"""screener 纯逻辑单元测试（mock 网络层，无真实请求）。"""

import json

import pytest

from tradingagents.shortterm import screener


def _row(code, name="测试股", mcap_yi=100.0, amount=5.0, industry="半导体"):
    return {
        "code": code, "name": name, "price": 10.0, "chg_pct": 3.0,
        "turnover_pct": 8.0, "vol_ratio": 2.0, "amount_yi": amount,
        "mcap_yi": mcap_yi, "float_mcap_yi": mcap_yi * 0.7,
        "main_net_inflow_yi": 0.5, "main_net_inflow_pct": 1.2,
        "industry": industry,
    }


def _tier_row(code, industry="半导体", activity=0.5, limit=0, ret7=3.0,
              inflow=0.5, anomalies=None):
    r = _row(code, industry=industry)
    r["activity_score"] = activity
    r["ch0"] = {
        "limit_up_streak": limit, "ret_7d_pct": ret7,
        "main_net_inflow_yi": inflow,
        "anomalies": anomalies or [],
    }
    return r


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
        monkeypatch.setattr(screener, "fetch_market_sentiment",
                            lambda d=None: {"sentiment": "unknown", "label": "数据缺失"})
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
        monkeypatch.setattr(screener, "_cost",
                            type("_FC", (), {"cost_context": lambda *a, **k: []})())

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
        monkeypatch.setattr(screener, "fetch_market_sentiment",
                            lambda d=None: {"sentiment": "unknown", "label": "数据缺失"})

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
        monkeypatch.setattr(screener, "fetch_market_sentiment",
                            lambda d=None: {"sentiment": "unknown", "label": "数据缺失"})

        screener.scan(per_board=5)
        assert seen["lhb_count"] is None  # 批量失败 → 逐票回退
        assert seen["quote"]["name"] == "测试股"  # 快照 quote 注入生效


class TestAssignTiers:
    """板块梯队五档：龙头/龙二龙三/中位股/低位补涨/跟风杂毛（《投资分析体系》v1.1）。"""

    def test_full_five_tiers_in_one_board(self):
        rows = [
            _tier_row("600001", activity=1.0, limit=2),      # 龙头
            _tier_row("600002", activity=0.9, limit=1),      # 龙二
            _tier_row("600003", activity=0.8, limit=1),      # 龙三
            _tier_row("600004", activity=0.5, limit=2),      # 中位股（2板非前三）
            _tier_row("600005", activity=0.4, limit=0, ret7=8),  # 低位补涨
            _tier_row("600006", activity=0.3, limit=0, ret7=1),  # 跟风杂毛
        ]
        screener.assign_tiers(rows)
        assert rows[0]["tier"] == "龙头"
        assert rows[1]["tier"] == "龙二"
        assert rows[2]["tier"] == "龙三"
        assert rows[3]["tier"] == "中位股"
        assert "A杀" in rows[3]["tier_reason"]
        assert rows[4]["tier"] == "低位补涨"
        assert rows[5]["tier"] == "跟风杂毛"

    def test_no_leader_board(self):
        rows = [
            _tier_row("600101", activity=1.0, limit=0),
            _tier_row("600102", activity=0.5, limit=0, ret7=8),
        ]
        screener.assign_tiers(rows)
        assert rows[0]["tier"] == "龙头"  # 无涨停也取 activity 第一
        assert "无涨停" in rows[0]["tier_reason"]
        assert rows[1]["tier"] == "跟风杂毛"  # 无龙头涨停 → 不算低位补涨

    def test_leader_is_first_limit_up_not_activity_first(self):
        rows = [
            _tier_row("600201", activity=1.0, limit=0),   # 活跃第一但没涨停
            _tier_row("600202", activity=0.9, limit=3),   # 连板3板才是龙头
        ]
        screener.assign_tiers(rows)
        assert rows[0]["tier"] == "跟风杂毛"  # 放量没涨停=活跃杂毛
        assert rows[1]["tier"] == "龙头"

    def test_groups_are_independent_per_industry(self):
        rows = [
            _tier_row("600301", industry="半导体", activity=1.0, limit=2),
            _tier_row("600302", industry="半导体", activity=0.3, limit=0, ret7=1),
            _tier_row("600303", industry="白酒", activity=1.0, limit=1),
            _tier_row("600304", industry="白酒", activity=0.9, limit=0, ret7=9),
        ]
        screener.assign_tiers(rows)
        assert rows[0]["tier"] == "龙头"
        assert rows[1]["tier"] == "跟风杂毛"
        assert rows[2]["tier"] == "龙头"
        assert rows[3]["tier"] == "低位补涨"

    def test_empty_and_missing_industry(self):
        assert screener.assign_tiers([]) == []
        rows = [
            _tier_row("600401", industry="", activity=1.0, limit=2),
            _tier_row("600402", industry="", activity=0.4, limit=0, ret7=9),
        ]
        screener.assign_tiers(rows)
        assert rows[0]["tier"] == "龙头"
        assert rows[1]["tier"] == "低位补涨"

    def test_four_plus_limit_not_in_top3_is_mid(self):
        rows = [
            _tier_row("600501", activity=1.0, limit=3),
            _tier_row("600502", activity=0.9, limit=2),
            _tier_row("600503", activity=0.8, limit=2),
            _tier_row("600504", activity=0.6, limit=5),  # 高位连板非前排
        ]
        screener.assign_tiers(rows)
        assert rows[3]["tier"] == "中位股"


class TestMarketSentiment:
    """情绪周期温度计（涨停家数/最高连板/跌停家数 → 五档）。"""

    def _fake_em_get(self, pool_rows, down_chgs):
        def fake(url, params=None, headers=None, timeout=15, **kw):
            if "getTopicZTPool" in url:
                return _FakeResp({"data": {"pool": pool_rows}})
            return _FakeResp({"data": {"diff": [
                {"f12": f"60{i:04d}", "f3": c} for i, c in enumerate(down_chgs)
            ]}})
        return fake

    def test_ice_point(self, monkeypatch):
        pool = [{"c": "600001", "lbc": 2} for _ in range(15)]
        down = [-9.9] * 30
        monkeypatch.setattr(screener, "_em_get",
                            self._fake_em_get(pool, down))
        s = screener.fetch_market_sentiment("2026-08-04")
        assert s["limit_up_count"] == 15
        assert s["max_streak"] == 2
        assert s["label"] == "冰点"

    def test_climax(self, monkeypatch):
        pool = [{"c": "600001", "lbc": 1} for _ in range(150)]
        monkeypatch.setattr(screener, "_em_get",
                            self._fake_em_get(pool, []))
        s = screener.fetch_market_sentiment("2026-08-04")
        assert s["label"] == "高潮"

    def test_retreat_high_streak_low_count(self, monkeypatch):
        pool = [{"c": "600001", "lbc": 5} for _ in range(15)]
        down = [-9.9] * 25
        monkeypatch.setattr(screener, "_em_get",
                            self._fake_em_get(pool, down))
        s = screener.fetch_market_sentiment("2026-08-04")
        assert s["label"] == "退潮"

    def test_recovery(self, monkeypatch):
        pool = [{"c": "600001", "lbc": 2} for _ in range(25)]
        monkeypatch.setattr(screener, "_em_get",
                            self._fake_em_get(pool, []))
        s = screener.fetch_market_sentiment("2026-08-04")
        assert s["label"] == "修复"

    def test_heating(self, monkeypatch):
        pool = [{"c": "600001", "lbc": 3} for _ in range(60)]
        monkeypatch.setattr(screener, "_em_get",
                            self._fake_em_get(pool, []))
        s = screener.fetch_market_sentiment("2026-08-04")
        assert s["label"] == "升温"

    def test_unknown_on_failure(self, monkeypatch):
        def boom(url, **kw):
            raise RuntimeError("network down")
        monkeypatch.setattr(screener, "_em_get", boom)
        s = screener.fetch_market_sentiment("2026-08-04")
        assert s["sentiment"] == "unknown"
        assert s["label"] == "数据缺失"


class _FakeResp:
    def __init__(self, payload):
        self._p = payload

    def json(self):
        return self._p


class TestThemeLifecycle:
    """主题生命周期（P5）：concepts 聚合频次 + prompt 生命周期指令。"""

    def test_theme_stats_in_prompt(self, monkeypatch):
        captured = {}

        class _FakeLLM:
            def invoke(self, prompt):
                captured["prompt"] = prompt
                return type("R", (), {"content": "ok"})()

        monkeypatch.setattr(screener, "build_llm", lambda *a, **k: _FakeLLM())
        monkeypatch.setattr(screener, "_cost",
                            type("_FC", (), {"cost_context": lambda *a, **k: []})())

        rows = [
            _row("600001"), _row("600002"), _row("600003"),
        ]
        rows[0]["concepts"] = "人工智能/算力"
        rows[1]["concepts"] = "人工智能/液冷"
        rows[2]["concepts"] = "白酒"
        scan_result = {
            "trade_date": "2026-08-01", "capital": 200000,
            "boards": {"main": rows, "cyb": [], "kcb": []},
            "rejected": {"main": [], "cyb": [], "kcb": []},
        }
        screener.recommend(scan_result, "deepseek", "deepseek-chat")

        prompt = captured["prompt"]
        assert "人工智能(2票)" in prompt      # 频次聚合注入
        assert "主题生命周期" in prompt        # 生命周期指令存在
        assert "兴起" in prompt and "退潮" in prompt
        assert "过热" in prompt and "兑现" in prompt
