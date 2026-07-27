from __future__ import annotations

import json
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


def test_chrome_dashboard_gate_is_persisted_under_tests() -> None:
    script = (PROJECT_ROOT / "tests" / "performance" / "run_chrome_dashboard.py").read_text(encoding="utf-8")

    assert 'REPORT_SCHEMA = "chrome-dashboard-performance-v1"' in script
    assert "VIEWPORTS = ((1280, 720), (1440, 900), (1920, 1080))" in script
    assert "pipeline_d4_browser_fixture" in script
    assert "WEB_ASSET_REVISION" in script
    assert "patchToPaint" in script
    assert "browserErrors" in script
    assert "/tmp/trader_cdp" not in script


def test_docs_keep_two_authorities_and_pipeline_reports() -> None:
    docs_root = PROJECT_ROOT / "docs"
    documents = sorted(path.relative_to(docs_root).as_posix() for path in docs_root.rglob("*") if path.is_file())

    expected_reports = {
        "reports/a-share-long-industry-research-2026-07-24.md",
        "reports/chokepoint-watchlist-document-split-2026-07-25.md",
        "reports/long-watchlist-changes-2026-07-25.md",
        "reports/pipeline-a1-baseline.md",
        "reports/pipeline-a2-public-skeleton.md",
        "reports/pipeline-a3-integration.md",
        "reports/pipeline-a4-acceptance.md",
        "reports/pipeline-a5-final-review.md",
        "reports/pipeline-d1-p6-web.md",
        "reports/pipeline-g1-contract-base.md",
        "reports/pipeline-g2-gate-review.md",
        "reports/pipeline-g3-gate-review.md",
        "reports/pipeline-g4-gate-review.md",
        "reports/pipeline-g5-final-gate.md",
        "reports/solid-state-watchlist-merge-2026-07-25.md",
    }
    assert set(documents) == {
        "recommendation-strategy.md",
        "software-business-design.md",
        *expected_reports,
    }

    design = (docs_root / "software-business-design.md").read_text(encoding="utf-8")
    report = (docs_root / "reports/pipeline-a1-baseline.md").read_text(encoding="utf-8")
    strategy = (docs_root / "recommendation-strategy.md").read_text(encoding="utf-8")
    assert "软件业务设计文档" in design
    assert "荐股策略文档" in strategy
    assert "已实施实时与降级基线" in design
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
    for retired_plan in ("plan.md", "plan_c.md", "plan_sudu.md", "plan_pipeline.md"):
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
    assert "重启真实" in matrix
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
    assert "盘中当前视图只显示 `executable`" in selection
    assert "Web 必须分栏展示" not in selection

    fallback_reason = "close_fallback_observe_floor"
    retired_fallback_reason = "close_fallback_observation_floor_relaxed"
    assert fallback_reason in strategy
    assert fallback_reason in design
    assert retired_fallback_reason not in strategy
    assert retired_fallback_reason not in design

    midday_topk = runtime["pipeline"]["cadence_seconds"]["topk_quotes"]["midday"]
    assert f"{midday_topk} 秒 TopK" in timeline
    assert "| 午间 | 10秒 | 10秒 | 10秒 |" in cadence
    assert "每板最多 120 只、三板合计最多 360 只" in cadence

    assert strategy_config["selection"]["minimum_board_reliability"] == 0.85
    assert "板块人口不足" in persistence
    assert "板块可靠度不足只降为观察" in persistence
    assert "收盘补算和历史视图 显示 API 返回的全部项" in " ".join(web.split())

    p6_capacity = runtime["market_data"]["cache_policy"]["datasets"]["published_recommendation_view"]["capacity"]
    assert f"published_recommendation_view.capacity={p6_capacity}" in cache_limits
    assert "`4 + 20 * 3 = 64`" in cache_limits
    assert "活动 `/api/status` 只承诺代码已经聚合的运行事实" in observability
    assert "不承诺尚未实现的平均批次大小" in observability
    assert "`trader-cli perf-check` 及发布验收报告提供" in observability


def _section(path: Path, start: str, end: str) -> str:
    content = path.read_text(encoding="utf-8")
    return content.split(start, 1)[1].split(end, 1)[0]
