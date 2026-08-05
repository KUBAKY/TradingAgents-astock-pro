# TradingAgents-Astock

## 项目概述
基于 [TauricResearch/TradingAgents](https://github.com/TauricResearch/TradingAgents)（65K Stars）的 A 股深度特化 fork。多 Agent 投研框架，7 个 Analyst 角色通过 Bull/Bear 辩论 + 三方风险辩论生成投资报告。

- **仓库**: https://github.com/simonlin1212/TradingAgents-astock
- **协议**: Apache 2.0
- **Python**: >=3.10
- **当前版本**: 0.4.0

## 架构

### 数据层（v0.2.5 全部直连 HTTP，零第三方数据库依赖）
| 来源 | 协议 | 数据 |
|------|------|------|
| mootdx | TCP 7709 | OHLCV K线、财务快照、F10 文本 |
| 腾讯财经 | HTTP (qt.gtimg.cn) | PE/PB/市值/换手率 |
| 东方财富 datacenter | HTTP (datacenter-web) | 龙虎榜、限售解禁、板块行情 |
| 东方财富 push2/push2his | HTTP (push2.eastmoney) | 实时行情、个股信息、板块列表、资金流(分钟+日级) |
| 东方财富 np-weblist | HTTP | 滚动新闻 |
| 新浪财经 | HTTP (money.finance.sina) | K线历史、财报三表 |
| 同花顺 10jqka | HTTP | EPS 一致预期、热股题材 |
| 财联社 cls.cn | HTTP | 全球财经快讯 |
| 百度股市通 | HTTP (gushitong.baidu) | 概念板块归属（资金流已迁移至东财push2） |

### Agent 角色（7 个）
原版 4 个（市场/情绪/新闻/基本面）+ A 股特化 3 个（政策分析师/游资追踪/解禁监控）

### 关键路径
- `tradingagents/dataflows/a_stock.py` — A 股数据 vendor，所有数据获取入口
- `tradingagents/dataflows/utils.py` — `safe_ticker_component` 路径安全校验 + 中文 ticker 自动解析
- `tradingagents/agents/` — 7 个 Analyst + Bull/Bear 辩论逻辑
- `web/` — Streamlit Web UI
- `cli/` — CLI 入口

### 短线交易分析系统（第二产品线，2026-08 起）
- `tradingagents/shortterm/` — 短线决策闭环：ch0 异动精扫、pipeline 决策、screener 全市场扫描、portfolio 持仓跟进（割/持/补，幂等落盘 snapshots/）、history 落盘+事后评估、prompts（决策卡/自校准注入）
- `web/pages/1_短线分析.py` + `web/runner.py` — 短线 UI 与 `stream_mode="updates"` 事件式进度
- `web/pages/2_持仓管理.py` — 持仓 CRUD + 每日跟进 + 快照盈亏仪表盘
- `scripts/close_scan.py`（launchd 盘后自动扫描入口）、`scripts/close_portfolio.py`（launchd 盘后自动持仓跟进）、`scripts/check_auction.py`（集合竞价验证）
- 决策落盘目录 `~/.tradingagents/shortterm/`（个人数据，不入库）；调度日志 `~/.tradingagents/logs/`

### 统一分析结果注册表（2026-08 起，⑪）
- `tradingagents/analysis_registry/` — 六类分析结果统一索引与闭环（KINDS=stock/follow/screen/pick/deep_review/mainline）：`registry.py`（record_id=`<kind>:<ticker>:<trade_date>:<ts>` 幂等注册 + 首次 backfill，4 个注册点 wrapper 已接：短线 pipeline/screener/portfolio、deep_review）、`compare.py`（after_register 同票最近两条自动规则对比 → `compare/*.json`；LLM 深度对比仅手动+成本记账）、`validate.py`（盘后自动验证：stock/follow=3 交易日其余=10，幂等三态，rating→方向映射，K 线补齐基准收盘副本不回写，单条降级不抛）、`feedback.py`（误差报告 `reports/feedback_<asof>.md` + 同方向错判≥50% 纪律注入短线 pipeline v2.1，失败空串零干扰）
- 目录 `~/.tradingagents/analysis_registry/`（`TRADINGAGENTS_REGISTRY_DIR` 覆盖；源目录 `TRADINGAGENTS_SHORTTERM_DIR`/`_LOGS_DIR`/`_DEEPREVIEW_DIR` 供测试隔离）
- `scripts/run_validations.py`（launchd 工作日 15:35 已装 com.tradingagents.validate）、`scripts/run_feedback.py`
- `web/pages/3_分析对比.py` — 同票跨方式时间线（含验证列）/规则对比/手动 LLM 对比/手动补验证/误差报告；`web/pages/1_短线分析.py` 复盘 tab 有 page_link 入口
- 测试：`tests/test_analysis_registry.py`(21)/`test_compare.py`(17)/`test_validate.py`(21)/`test_feedback.py`(10)；conftest autouse 四目录 tmp 隔离
- 治理与进度：`docs/GOVERNANCE.md`、`DEV_LOG.md`（短线系统建设期节）

### 中文股票名解析链路
用户/LLM 输入 → `safe_ticker_component` 检测中文 → `resolve_ticker()` → `_build_name_code_map()`（mootdx 全市场映射，缓存）→ 返回 6 位代码

## 已知问题与注意事项

### 依赖冲突（v0.2.6 已缓解，v0.4.0 再解一层）
mootdx 钉死 `httpx>=0.25,<0.26`，与 langchain-google-genai 所需的 `httpx>=0.28.1` **结构性冲突**（该区间内每个 google-genai 版本都要 0.28.1，无解）。[google] extra 已移除（#87），详见 pyproject.toml 注释。

**claude-agent-sdk ↔ mootdx 同源冲突（v0.4.0 已解）**：sdk（含最新 0.2.130）声明 `mcp<2.0.0`，而 mcp 1.x 全部要求 `httpx>=0.27.1`，与 mootdx 的 `httpx<0.26` 无解冲突（sdk→mcp→httpx 链必然撞 mootdx）。解法：`[tool.uv] override-dependencies = ["httpx>=0.27.1,<0.28"]` 强制 httpx 0.27.x（mootdx 的 httpx 仅用于复权/节假日工具模块，核心 K 线走 pytdx TCP，0.25→0.27 无破坏性变更）。⚠️ 切勿升 sdk 到 mcp 2.0 兼容版（不存在）；mcp 2.0.0 会让 sdk 的 `create_sdk_mcp_server` 炸 `Server.list_tools` AttributeError（mcp 2.0 顶层 Server 无该方法）。

**⚠️ 新增依赖后务必跑 `uv lock` 验证**——pip 能装通不代表 uv 能锁。

### akshare 已移除（v0.2.5）
v0.2.5 起完全移除 akshare 依赖，所有数据通过直连 HTTP API 获取。

### 百度 PAE 资金流接口已下线（v0.2.7 已修复）
`fundsortlist` 和 `fundflow` 两个接口返回空（2026-05-19 确认）。v0.2.7 已替换为东财 push2 资金流 API。同时修复了 `RPT_ORGANIZATION_BUSSINESS`（改用席位筛选机构）和东财全球资讯 `req_trace` 参数。

### 东财接口防封限流（v0.2.11 新增，移植自 a-stock-data v3.2）
`a_stock.py` 里所有指向 `eastmoney.com` 的请求（push2 / push2his / datacenter-web / search-api / np-weblist 共 7 个调用点）统一走节流入口 `_em_get()`：模块级时间戳串行限流（默认间隔 `EM_MIN_INTERVAL=1.0s`，可用同名环境变量覆盖）+ 0.1~0.5s 随机抖动 + 复用 `requests.Session`（Keep-Alive）+ 默认 UA。多 Agent 跑批量分析不再触发东财临时封 IP。**仅东财限流**——mootdx(TCP) / 腾讯 / 新浪 / 同花顺 / 财联社 / 百度 等非东财源不受影响。批量场景可设 `EM_MIN_INTERVAL=1.5~2` 进一步降速。新增东财端点时务必走 `_em_get` 而非裸 `requests.get`。

### 模型兼容性
deepseek-v4-flash 等模型在 tool call 时可能返回中文股票名而非 6 位代码。`safe_ticker_component` 已加兜底自动转码，但不同模型表现仍有差异。

### 待处理 PR
- PR #18（hejingchi）：start_date 功能 + 主题切换 + Windows 字体。不建议直接 merge（与 v0.2.6 冲突），start_date 功能值得后续自行实现。

## Issue 归档
所有 GitHub Issue 的详细记录在 `issues/` 文件夹中，包含问题描述、根因分析、修复方案和当前状态。

## 开发规范
- 改动前先跑 `python -m pytest tests/ -v` 确保不破坏现有测试
- `safe_ticker_component` 是安全边界，任何绕过路径校验的改动必须慎重评估
- 数据层新增接口遵循 `tradingagents/dataflows/interface.py` 的 vendor 路由模式
- Web UI 改动在 `web/` 目录，用 `streamlit run web/launch.py` 本地测试

## 相关项目
- [a-stock-data](https://github.com/simonlin1212/a-stock-data) — A 股 MCP 数据服务（Claude Code 用的 skill）
- 上游 [TauricResearch/TradingAgents](https://github.com/TauricResearch/TradingAgents) — 原版框架
