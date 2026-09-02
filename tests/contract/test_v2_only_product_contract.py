from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_authoritative_design_requires_one_final_v2_product_chain_without_runtime_compatibility() -> None:
    design = (ROOT / "docs/software-business-design.md").read_text(encoding="utf-8")

    for statement in (
        "V2-only 最终 release 边界",
        "当前交付状态：V2-only 工程与发布门禁验收已闭合",
        "新 release 不读取旧运行目录、旧数据库、旧快照或旧 schema",
        "V2 唯一运行目录固定为 `.runtime/v2`",
        "旧 release 只能与其对应旧运行目录整体回退",
        "不得提供旧 API 别名、重定向、弃用窗口、双读或双写",
        "GET /api/v2/decisions/<strategy>/current",
        "GET /api/v2/decisions/<strategy>/history?date=YYYY-MM-DD",
        "GET /api/v2/decisions/<strategy>/dates",
        "GET /api/v2/status",
        "GET /api/v2/events",
    ):
        assert statement in design


def test_strategy_contract_requires_v2_native_decisions_and_no_legacy_replay() -> None:
    strategy = (ROOT / "docs/recommendation-strategy.md").read_text(encoding="utf-8")
    compact = " ".join(strategy.split())

    for statement in (
        "最终 V2 评分口径只产生 `ScoredDecision`",
        "新 release 不得构造或读取旧 `RecommendationSnapshot`",
        "不回放旧策略、旧引擎或旧 schema",
        "long 不借用评分字段伪造荐股决策形状",
    ):
        assert statement in compact


def test_parallel_v2_overview_plan_and_operations_docs_are_retired() -> None:
    design = (ROOT / "docs/software-business-design.md").read_text(encoding="utf-8")
    strategy = (ROOT / "docs/recommendation-strategy.md").read_text(encoding="utf-8")
    design_compact = " ".join(design.split())

    assert "V2-only 工程与发布门禁验收已闭合" in design
    assert "当前代码仍属于 `Unreleased`" in design
    assert "只有用户显式发起独立发布批次" in design
    assert "新 release 不读取旧运行目录、旧数据库、旧快照或旧 schema" in design
    assert "原生评分因子诊断层" in design
    assert "多种行情来源不等于证券主数据存在同等冗余供给" in design
    assert "主推荐区必须按确定性优先级给出单一结论" in design_compact
    assert "达到观察线/正式线数量" in design_compact
    assert "当前可配置 Tomorrow 模型允许展示评分版本" in design_compact
    assert "loss_probability_status=not_modeled" in design_compact
    assert "P2 路线已经终止，当前没有可继续晋级的 P2 候选" in strategy
    assert "新的候选必须另立未读取新收益的研究身份" in strategy
    for retired in (
        "V2.md",
        "implementation-plan.md",
        "start_stop.md",
        "review.md",
        "fenshu.md",
    ):
        assert not (ROOT / "docs" / retired).exists()
    assert not (ROOT / "docs/V2_plan.md").exists()
    assert not (ROOT / "docs/score.md").exists()
    assert not (ROOT / "docs/plan.md").exists()


def test_release_guides_expose_only_v2_runtime_and_desktop_gate() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    operations = (ROOT / "docs/software-business-design.md").read_text(encoding="utf-8")

    for content in (readme, operations):
        assert "/api/v2/status" in content
        assert "/api/status" not in content
        assert "trader-cli perf-check" not in content
    assert "deepseek-budget.sqlite3" in operations
    assert "卡脖子、高成长、低价潜力" in operations
    for public_command in ("./run.sh check", "./run.sh download_history", "./run.sh train-tomorrow"):
        assert public_command in readme
        assert public_command in operations
    for retired_command in ("./run.sh validate-config", "./run.sh performance-check"):
        assert retired_command not in readme
        assert retired_command not in operations
    assert "curl -fsS http://127.0.0.1:5000/api/v2/status" in operations
    assert (ROOT / "tests/performance/run_desktop_dashboard.py").is_file()
    assert not (ROOT / "tests/performance/run_chrome_dashboard.py").exists()
