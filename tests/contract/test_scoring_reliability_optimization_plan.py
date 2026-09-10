from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STRATEGY = ROOT / "docs/01_评分逻辑.md"
DESIGN = ROOT / "docs/02_工程设计.md"
WORK = ROOT / "docs/03_工程实施.md"
REPLAY = ROOT / "docs/04_策略回溯.md"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_authoritative_docs_define_one_ordered_optimization_route() -> None:
    strategy = _read(STRATEGY)
    design = _read(DESIGN)
    work = _read(WORK)
    ordered_tasks = (
        "## 2. 当前执行章节：V3 训练工件重建与整组发布",
        "## 3. V3 工程运行验收",
        "## 4. 点时证据修复",
        "## 5. 候选容量与排序历史验证",
        "## 6. 一次性终端留出与 Shadow",
        "## 7. 人工候选策略生产启用",
    )
    positions = tuple(work.index(task) for task in ordered_tasks)
    assert positions == tuple(sorted(positions))
    assert "收益真值" in strategy
    assert "评分优化目标组件" in design


def test_strategy_catalog_covers_realtime_reliability_and_return_families() -> None:
    strategy = _read(STRATEGY)

    for required in (
        "数据质量与执行真值",
        "日内价格与成交路径",
        "流动性、容量与尾部风险",
        "财务质量与事件事实",
        "市场与板块状态",
        "预测不确定性",
        "CandidateRecallLedger",
        "历史 oracle TopK recall",
        "OOD 距离",
        "模型分歧",
        "published_at",
        "effective_at",
    ):
        assert required in strategy


def test_design_assigns_narrow_typed_component_owners() -> None:
    design = _read(DESIGN)

    for required in (
        "CanonicalOutcomeEvaluator",
        "FeatureSpecCatalog",
        "FeatureCalculators",
        "FeatureComputationPlan",
        "FeatureVectorManifest",
        "CandidateRecallLedger",
        "AlphaScore",
        "RiskDecision",
        "ExecutionCost",
        "SelectionUtility",
        "ShadowMonitor",
        "FeatureId",
        "FeatureValue",
    ):
        assert required in design
    assert "SelectionUtility` 首先只属于隔离研究与 Shadow" in design


def test_incremental_feature_computation_is_complete_and_observable_without_a_plan_hash() -> None:
    design = _read(DESIGN)
    changelog = _read(ROOT / "CHANGELOG.md")
    combined = " ".join((design + changelog).split())

    for required in (
        "incremental_feature_computation",
        "FeatureFactRevision",
        "FeatureStageInvalidation",
        "普通价格 overlay",
        "5500 行全市场、360 个候选",
        "deadline 放弃原因",
    ):
        assert required in combined
    assert "`tomorrow_model.computation` 只允许包含" in design
    assert "`FeatureComputationPlan`" in design
    assert "不生成无人消费的" in design


def test_route_cannot_smuggle_unverified_data_or_automatic_promotion() -> None:
    strategy = _read(STRATEGY)
    work = _read(WORK)
    replay = _read(REPLAY)

    for required in (
        "不得用 15:00 收盘价冒充 14:50",
        "不得恢复未来日 collector",
        "不可靠主力资金",
        "DeepSeek 自由文本",
        "不得自动训练",
        "必须由用户明确授权",
    ):
        assert required in strategy + work + replay
    assert "terminal_holdout_and_shadow" in work
    assert "manual_candidate_strategy_activation" in work


def test_changelog_records_completed_fail_closed_dataset_and_recall_code() -> None:
    work = _read(ROOT / "CHANGELOG.md")

    for token in (
        "point_in_time_data_qualification",
        "point_in_time_dataset",
        "candidate_recall_attribution",
        "historical_data_insufficient",
        "v3_single_cost_ownership",
        "D25",
    ):
        assert token in work


def test_point_in_time_qualification_has_three_independent_gates_and_no_production_authority() -> None:
    strategy = _read(STRATEGY)
    design = _read(DESIGN)
    work = _read(WORK)
    replay = _read(REPLAY)
    changelog = _read(ROOT / "CHANGELOG.md")
    combined = strategy + design + work + replay + changelog

    for required in (
        "PointInTimeDataQualificationReport",
        "历史分钟",
        "至少 300 只",
        "11:20/14:50",
        "raw/qfq",
        "historical_data_insufficient",
        "production_authority=false",
    ):
        assert required in combined
    assert "5453/5453" in changelog
    assert "9,085,235" in changelog
    assert "99.9976%" in changelog
    assert "分钟点时" in work


def test_history_training_and_optimization_are_one_dependency_route() -> None:
    work = _read(WORK)
    replay = _read(REPLAY)

    assert "baostock_increment_archive" not in work
    assert "v3_training_artifact_rebuild" in work
    assert "dynamic_cutoff_and_missing_fact_acquisition" not in work
    assert "动态 `source_cutoff`、精确滚动" in replay
    assert "15:00_close" in replay
    assert "point_in_time_parity=false" in replay
    assert "BaoStock 日线不能单独证明 14:50" in replay
    assert "FeatureVectorManifest" in replay
