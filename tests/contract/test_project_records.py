from __future__ import annotations

import json
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_continue_command_advances_one_complete_unfinished_section() -> None:
    agents = _section(PROJECT_ROOT / "AGENTS.md", "### 4.1", "### 4.2")
    design = _section(PROJECT_ROOT / "docs/software-business-design.md", "### 15.1", "### 15.2")

    for contract in (agents, design):
        assert "下一个完整未完成章节" in contract
        assert "章节内全部明确子项" in contract
        assert "相邻章节" in contract
        assert "下一个未完成的最小可独立验收任务" not in contract


def test_each_pipeline_documents_user_problem_and_change_summary() -> None:
    agents = _section(PROJECT_ROOT / "AGENTS.md", "### 4.5", "## 5")
    design = _section(PROJECT_ROOT / "docs/software-business-design.md", "### 15.1", "### 15.2")

    for contract in (agents, design):
        assert "用户提出的问题" in contract
        assert "修改说明" in contract
        assert "验证证据" in contract
        assert "剩余风险" in contract
        assert "CHANGELOG.md" in contract


def test_final_v2_api_sse_web_contract_and_current_delivery_state_are_recorded() -> None:
    design = (PROJECT_ROOT / "docs/software-business-design.md").read_text(encoding="utf-8")

    assert "### 2.6 V2 查询与发布" in design
    assert "UnifiedDecisionIndex -> application queries -> /api/v2 -> SSE -> Web" in design
    assert "GET /api/v2/decisions/<strategy>/current" in design
    assert "GET /api/v2/decisions/<strategy>/history?date=YYYY-MM-DD" in design
    assert "统一公开外壳已交付" in design
    assert "publisher 不等待客户端消费" in design
    assert "`bootstrap.py`" in design
    runner = PROJECT_ROOT / "tests/performance/run_chrome_dashboard.py"
    script = runner.read_text(encoding="utf-8")
    assert "VIEWPORTS = ((1280, 720), (1440, 900), (1920, 1080))" in script
    assert 'REPORT_SCHEMA = "unified-v2-browser-v1"' in script


def test_chrome_dashboard_gate_is_persisted_under_tests() -> None:
    script = (PROJECT_ROOT / "tests" / "performance" / "run_chrome_dashboard.py").read_text(encoding="utf-8")
    assert 'REPORT_SCHEMA = "unified-v2-browser-v1"' in script
    assert "VIEWPORTS = ((1280, 720), (1440, 900), (1920, 1080))" in script
    assert "pipeline_d4_browser_fixture" in script
    assert "WEB_ASSET_REVISION" in script
    assert "TraderV2Diagnostics" in script
    assert '"long_viewports"' in script
    assert 'button[data-strategy=\\"long\\"]' in script
    assert "/tmp/trader_cdp" not in script


def test_docs_keep_two_authorities_and_pipeline_reports() -> None:
    docs_root = PROJECT_ROOT / "docs"
    documents = tuple(
        line.removeprefix("docs/")
        for line in subprocess.run(
            ["git", "ls-files", "docs"],
            cwd=PROJECT_ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.splitlines()
    )

    expected_reports = {
        "reports/a-share-long-industry-research-2026-07-24.md",
        "reports/chokepoint-watchlist-document-split-2026-07-25.md",
        "reports/long-watchlist-changes-2026-07-25.md",
        "reports/pipeline-a1-baseline.md",
        "reports/pipeline-a2-public-skeleton.md",
        "reports/pipeline-a3-integration.md",
        "reports/pipeline-a4-acceptance.md",
        "reports/pipeline-a5-final-review.md",
        "reports/v2-p0-baseline.md",
        "reports/pipeline-d1-p6-web.md",
        "reports/pipeline-g1-contract-base.md",
        "reports/pipeline-g2-gate-review.md",
        "reports/pipeline-g3-gate-review.md",
        "reports/pipeline-g4-gate-review.md",
        "reports/pipeline-g5-final-gate.md",
        "reports/v2-p1-source-capability-baseline.md",
        "reports/solid-state-watchlist-merge-2026-07-25.md",
    }
    assert set(documents) == {
        "V2.md",
        "implementation-plan.md",
        "recommendation-strategy.md",
        "software-business-design.md",
        "start_stop.md",
        *expected_reports,
    }

    design = (docs_root / "software-business-design.md").read_text(encoding="utf-8")
    report = (docs_root / "reports/pipeline-a1-baseline.md").read_text(encoding="utf-8")
    implementation_plan = (docs_root / "implementation-plan.md").read_text(encoding="utf-8")
    v2_plan = (docs_root / "V2.md").read_text(encoding="utf-8")
    strategy = (docs_root / "recommendation-strategy.md").read_text(encoding="utf-8")
    assert "软件业务设计文档" in design
    assert "荐股策略文档" in strategy
    assert "V2-E0、V2-E1、V2-E2、V2-E3、V2-E4、V2-E5、V2-E6、V2-E7、V2-E8" in implementation_plan
    assert "下一工程章节为 V2-E9" in implementation_plan
    assert "`docs/recommendation-strategy.md`" in implementation_plan
    assert "本文是唯一活动施工计划" in implementation_plan
    assert "状态：V2-only 目标已确认" in v2_plan
    assert "`docs/software-business-design.md`" in v2_plan
    assert "`docs/recommendation-strategy.md`" in v2_plan
    assert "`docs/implementation-plan.md`" in v2_plan
    assert "`docs/software-business-design.md`" in implementation_plan
    assert "`docs/recommendation-strategy.md`" in implementation_plan
    assert "### V2-E0：唯一产品契约重置（已完成）" in implementation_plan
    assert "### V2-E10：删除旧生产链" in implementation_plan
    assert "### V2-E11：最终验收与发布" in implementation_plan
    assert "`docs/V2.md` 是用户明确保留的 V2 唯一产品目标概览" in design
    assert "评价最多 60 个不同交易日" in implementation_plan
    assert "研究不保存硬拒绝逐股身份" in implementation_plan
    assert "迁移过程、事故复盘和逐批实现" in design
    assert "已实施实时与降级基线" not in design
    assert "待验证收益路线" in strategy
    assert "docs/celue.md" not in design
    assert "docs/hi.md" not in design
    assert "docs/queston.md" not in design
    assert "A1.x 已完成本地基线采集与契约冻结" in report
    assert "G1 发布" in report
    assert "A2 public skeleton is available" in (docs_root / "reports/pipeline-a2-public-skeleton.md").read_text(
        encoding="utf-8"
    )
    assert "A3 integration handoff is available" in (docs_root / "reports/pipeline-a3-integration.md").read_text(
        encoding="utf-8"
    )
    assert "A4.1-A4.6" in (docs_root / "reports/pipeline-a4-acceptance.md").read_text(encoding="utf-8")
    assert "A5.1-A5.5" in (docs_root / "reports/pipeline-a5-final-review.md").read_text(encoding="utf-8")
    assert "G2 已发布" in (docs_root / "reports/pipeline-g2-gate-review.md").read_text(encoding="utf-8")
    assert "G3 已发布" in (docs_root / "reports/pipeline-g3-gate-review.md").read_text(encoding="utf-8")
    assert "G4 已发布" in (docs_root / "reports/pipeline-g4-gate-review.md").read_text(encoding="utf-8")
    assert "G5 已发布" in (docs_root / "reports/pipeline-g5-final-gate.md").read_text(encoding="utf-8")
    for retired_plan in (
        "V2_plan.md",
        "score.md",
        "plan.md",
        "plan_c.md",
        "plan_sudu.md",
        "plan_pipeline.md",
    ):
        assert not (docs_root / retired_plan).exists()
    assert "docs/need.md" not in design


def test_authoritative_docs_match_active_runtime_identities() -> None:
    runtime = json.loads((PROJECT_ROOT / "config/v2/runtime.json").read_text(encoding="utf-8"))
    strategy_config = json.loads((PROJECT_ROOT / "config/v2/strategy.json").read_text(encoding="utf-8"))
    design = (PROJECT_ROOT / "docs/software-business-design.md").read_text(encoding="utf-8")
    strategy = (PROJECT_ROOT / "docs/recommendation-strategy.md").read_text(encoding="utf-8")

    assert runtime["pipeline"]["decision_execution_mode"] == "versioned_dag"
    assert "`versioned_dag`" in design
    assert runtime["deepseek"]["daily_hard_limit"] == 168
    assert "全局硬上限 168" in strategy
    assert strategy_config["selection"]["review_candidate_limit"] == 28
    assert "最多 28 只" in design
    assert strategy_config["strategy_version"] in strategy


def test_authoritative_docs_define_ephemeral_observation_lifecycle() -> None:
    design = (PROJECT_ROOT / "docs/software-business-design.md").read_text(encoding="utf-8")
    strategy = (PROJECT_ROOT / "docs/recommendation-strategy.md").read_text(encoding="utf-8")

    for contract in (design, strategy):
        assert "09:30" in contract
        assert "11:20" in contract
        assert "14:50" in contract
        assert "纯内存" in contract
        assert "不展示" in contract
        assert "`executable`" in contract
        assert "`close_fallback`" in contract
    assert "不可变空记录" in design
    assert "不得用观察项补位" in design
    assert "回测" not in design
    assert "回测" in strategy


def test_authoritative_docs_define_startup_and_shutdown_lifecycle() -> None:
    design = (PROJECT_ROOT / "docs/software-business-design.md").read_text(encoding="utf-8")
    strategy = (PROJECT_ROOT / "docs/recommendation-strategy.md").read_text(encoding="utf-8")
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")

    for statement in (
        "ShutdownDeadline",
        "30 秒",
        "第二次关闭信号",
        "calendar_unavailable",
        "session generation",
        "1/2/5/10/30 秒",
    ):
        assert statement in design
    for statement in ("14:50（含）至 15:00（不含）", "FreezeAttempt", "相同对象"):
        assert statement in strategy
    assert "关闭浏览器不会停止" in readme


def test_authoritative_docs_define_the_v2_native_rebuild_contract() -> None:
    design = (PROJECT_ROOT / "docs/software-business-design.md").read_text(encoding="utf-8")
    strategy = (PROJECT_ROOT / "docs/recommendation-strategy.md").read_text(encoding="utf-8")

    for statement in (
        "tomorrow 是唯一最高优先级生产决策链",
        "DailyFeaturePack",
        "MarketEpoch",
        "CandidateQuoteEpoch",
        "ResearchEpoch",
        "ScoredDecision",
        "UnifiedDecisionIndex",
        "GET /api/v2/decisions/<strategy>/current",
        "GET /api/v2/decisions/<strategy>/history?date=YYYY-MM-DD",
        "GET /api/v2/decisions/<strategy>/dates",
        "GET /api/v2/status",
        "GET /api/v2/events",
        "已接收行情到本地预览提交 P95 不超过 5 秒",
        "本地预览到浏览器完成渲染 P95 不超过 1 秒",
        "全市场决策数据年龄 P95 不超过 10 秒",
        "DeepSeek 融合结果在本地预览后 P95 不超过 15 秒",
    ):
        assert statement in design
    assert "CurrentDecisionStore" not in design
    assert "冷启动历史预热、三策略异步评分、P1-P6" in design
    assert "不是产品目标或兼容要求" in design

    for statement in (
        "允许 0 到 6 只",
        "14:50 锚点至下一交易日收盘",
        "20bp、50bp、100bp",
        "总评价样本不得超过 60 个交易日",
        "20 个连续计划交易日",
        "前向阶段至少 100 条",
        "DeepSeek 继续参与融合分",
        "普通阶段全策略合计目标 36、硬上限 66",
        "tomorrow 独占目标 21、硬上限 38",
    ):
        assert statement in strategy
    assert "历史回放最多 40 个" in strategy
    assert "前向影子固定尝试 20 个连续" in strategy
    assert "CurrentDecisionStore" not in strategy


def test_authoritative_design_defines_free_hedged_full_market_route() -> None:
    design = (PROJECT_ROOT / "docs/software-business-design.md").read_text(encoding="utf-8")

    for statement in (
        "活动行情路由不得接入或自动尝试收费行情源",
        "先提交东方财富",
        "若东方财富失败或 1 秒仍未完成，立即提交新浪",
        "任一来源先返回",
        "完整有效结果就原子发布统一全市场索引",
        "3 次熔断 30 秒",
        "`physical_failure_count`",
        "`circuit_skipped_count`",
        "`superseded_count`",
    ):
        assert statement in design


def test_v2_rebuild_contract_is_direct_replacement_without_legacy_runtime() -> None:
    design = (PROJECT_ROOT / "docs/software-business-design.md").read_text(encoding="utf-8")

    assert "V2-only 最终 release 边界" in design
    assert "V2-E9 至 V2-E11 尚未交付" in design
    assert "新 release 不读取旧运行目录、旧数据库、旧快照或旧 schema" in design
    assert "V2 唯一运行目录固定为 `.runtime/v2`" in design
    assert "不得提供旧 API 别名、重定向、弃用窗口、双读或双写" in design
    assert "旧 release 只能与其对应旧运行目录整体回退" in design


def test_superseded_native_pipeline_migration_chronology_is_not_authoritative() -> None:
    design = (PROJECT_ROOT / "docs/software-business-design.md").read_text(encoding="utf-8")

    assert "tomorrow v2 原生输入驱动流水线交付边界" not in design
    assert "`RecommendationEngine.prepare_snapshot`" not in design
    assert "V2-E0 至 V2-E8 已把统一数据平面" in design
    compact = " ".join(design.split())
    assert "迁移期旧链仅可向已接管 策略提供同批不可变原生输入" in compact


def test_superseded_cutover_evidence_chronology_is_not_authoritative() -> None:
    design = (PROJECT_ROOT / "docs/software-business-design.md").read_text(encoding="utf-8")

    for statement in (
        "tomorrow v2 切换证据持久化与离线复核交付边界",
        "tomorrow-shadow-evidence.sqlite3",
        "`trader-cli tomorrow-cutover-evidence`",
        "`--require-eligible`",
    ):
        assert statement not in design
    assert "shadow、cutover、baseline 对比" in design


def test_tomorrow_data_plane_retention_is_documented_but_not_implemented() -> None:
    design = (PROJECT_ROOT / "docs/software-business-design.md").read_text(encoding="utf-8")
    source = PROJECT_ROOT / "src" / "trader"

    assert "压缩数据按交易日分区，默认保留 120 个交易日并设置 20GB 磁盘上限" in design
    assert "本阶段只保留文档契约，不实现磁盘归档、清理或容量驱逐代码" in design
    assert not (source / "infra" / "market_data" / "compressed_partitions.py").exists()
    assert not (source / "infra" / "persistence" / "market_epoch_archive.py").exists()


def test_tomorrow_deepseek_fusion_boundary_is_explicit() -> None:
    design = (PROJECT_ROOT / "docs/software-business-design.md").read_text(encoding="utf-8")
    strategy = (PROJECT_ROOT / "docs/recommendation-strategy.md").read_text(encoding="utf-8")

    for statement in (
        "tomorrow v2 DeepSeek 融合契约",
        "同一只读快照",
        "local `ScoredDecision`",
        "hybrid `ScoredDecision`",
        "融合结果只提交 `UnifiedDecisionIndex`",
    ):
        assert statement in design
    for statement in (
        "最多 28 只",
        "`deepseek_skipped_no_eligible_candidates`",
        "合法子集",
        "固定 68/32",
        "正式池最多 6 只、观察池最多 6 只",
    ):
        assert statement in strategy


def test_tomorrow_decision_index_and_freeze_boundary_is_explicit() -> None:
    design = (PROJECT_ROOT / "docs/software-business-design.md").read_text(encoding="utf-8")
    strategy = (PROJECT_ROOT / "docs/recommendation-strategy.md").read_text(encoding="utf-8")
    source = PROJECT_ROOT / "src/trader"

    for statement in (
        "tomorrow v2 决策索引与冻结",
        "`UnifiedDecisionIndex`",
        "`ScoredDecision`",
        "`V2DecisionCheckpoint`",
        "原子封口",
        "本用例不抓行情、不评分、不调用 DeepSeek",
    ):
        assert statement in design
    assert "旧 P6 baseline" not in design
    for statement in (
        "tomorrow v2 冻结选择与锚点",
        "`observed_at <= 14:50`",
        "冻结 local",
        "`selected=true`",
        "冷启动收盘补算只能提交 local",
    ):
        assert statement in strategy
    assert not (source / "application" / "current_decision_store.py").exists()


def test_ruff_guidance_names_the_active_lint_command() -> None:
    pyproject = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert "make lint-strict" not in pyproject
    assert "strict rule set in `make lint`" in pyproject


def test_recommendation_availability_regression_matrix_is_permanent() -> None:
    matrix = _section(PROJECT_ROOT / "docs/software-business-design.md", "### 13.1", "## 14.")

    for boundary in ("09:30-11:20", "11:20-13:00", "13:00-14:50", "14:50-15:00", "15:00 后"):
        assert boundary in matrix
    for strategy in ("today", "tomorrow", "d25", "long"):
        assert strategy in matrix
    for state in ("ready", "`not_ready`", "`close_fallback`"):
        assert state in matrix
    assert "热运行" in matrix
    assert "冷启动" in matrix
    assert "启动真实" in matrix
    assert "未替换旧进程" in matrix


def test_authoritative_docs_match_active_scoring_and_runtime_behavior() -> None:
    runtime = json.loads((PROJECT_ROOT / "config/v2/runtime.json").read_text(encoding="utf-8"))
    strategy_config = json.loads((PROJECT_ROOT / "config/v2/strategy.json").read_text(encoding="utf-8"))
    design = (PROJECT_ROOT / "docs/software-business-design.md").read_text(encoding="utf-8")
    strategy = (PROJECT_ROOT / "docs/recommendation-strategy.md").read_text(encoding="utf-8")

    filters = _section(PROJECT_ROOT / "docs/recommendation-strategy.md", "## 4.", "## 5.")
    scoring = _section(PROJECT_ROOT / "docs/recommendation-strategy.md", "## 7.", "## 8.")
    fusion = _section(PROJECT_ROOT / "docs/recommendation-strategy.md", "## 12.", "## 13.")
    selection = _section(PROJECT_ROOT / "docs/recommendation-strategy.md", "## 13.", "## 14.")
    timeline = _section(PROJECT_ROOT / "docs/software-business-design.md", "## 5.", "## 6.")
    cadence = _section(PROJECT_ROOT / "docs/software-business-design.md", "### 6.2", "### 6.3")
    persistence = _section(PROJECT_ROOT / "docs/software-business-design.md", "## 8.", "## 9.")
    web = _section(PROJECT_ROOT / "docs/software-business-design.md", "## 9.", "## 10.")
    cache_limits = _section(PROJECT_ROOT / "docs/software-business-design.md", "### 7.1", "### 7.2")
    observability = _section(PROJECT_ROOT / "docs/software-business-design.md", "## 11.", "## 12.")

    assert "必需阻断" in filters
    assert "可选告警/观察限制" in filters
    assert "board_classification_conflict" in filters
    assert "missing_listing_age_sessions" in filters

    assert "relative_strength_3d" in scoring
    assert "不过热不再是活动正向组件" in scoring
    assert "旧通用 d25 评分函数" in scoring

    assert "未应用有效 hybrid 时" in fusion
    assert "final_score = local_score" in fusion
    assert "单一数组" in selection
    assert "拆成正式推荐" in selection
    assert "独立观察池表" in selection

    retired_fallback_reasons = (
        "close_fallback_observe_floor",
        "close_fallback_observation_floor_relaxed",
    )
    assert "close_fallback_observe_floor" not in strategy
    assert "close_fallback_observe_floor" not in design
    assert all(reason not in strategy for reason in retired_fallback_reasons)
    assert all(reason not in design for reason in retired_fallback_reasons)

    midday_topk = runtime["pipeline"]["cadence_seconds"]["topk_quotes"]["midday"]
    assert f"{midday_topk} 秒 TopK" in timeline
    assert "| 午间 | 10秒 | 10秒 | 10秒 |" in cadence
    assert "每板最多 120 只、三板合计最多 360 只" in cadence

    assert strategy_config["selection"]["minimum_board_reliability"] == 0.85
    assert "板块人口不足" in persistence
    assert "没有正式推荐时仍创建" in persistence
    compact_web = " ".join(web.split())
    assert "冻结当前、 `close_fallback` 和显式历史只返回最多 6 项" in compact_web

    assert runtime["market_data"]["cache_policy"]["datasets"]["published_recommendation_view"]["capacity"] == 72
    assert "旧 Pipeline 阶段编号" in cache_limits
    assert "`GET /api/v2/status`" in observability
    assert "不承诺尚未 实现的指标" in " ".join(observability.split())
    assert "`trader-cli perf-check` 及发布验收报告提供" in " ".join(observability.split())


def _section(path: Path, start: str, end: str) -> str:
    content = path.read_text(encoding="utf-8")
    return content.split(start, 1)[1].split(end, 1)[0]
