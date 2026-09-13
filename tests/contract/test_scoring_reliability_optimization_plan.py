from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STRATEGY = ROOT / "docs/01_评分逻辑.md"
DESIGN = ROOT / "docs/02_工程设计.md"
WORK = ROOT / "docs/03_工程实施.md"
REPLAY = ROOT / "docs/04_策略回溯.md"
DELIVERY_HISTORY = ROOT / "docs/changelog/archive/legacy-through-2026-09-10.md"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_authoritative_docs_define_one_ordered_optimization_route() -> None:
    strategy = _read(STRATEGY)
    design = _read(DESIGN)
    work = _read(WORK)
    ordered_tasks = (
        "## 2. 共享多目标样本管线",
        "## 3. Today V3 模型头",
        "## 4. D25 V3 模型头",
        "## 5. train-v3 统一入口",
        "## 6. V3 多头运行评分",
        "## 7. 状态、API、SSE、Web 和冻结",
        "## 8. 真实全量训练",
        "## 9. 显式 V3 整链验收",
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
    changelog = _read(DELIVERY_HISTORY)
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
    assert "`computation` 只允许包含候选数" in design
    assert "`FeatureComputationPlan`" in design
    assert "不生成无人消费的" in design


def test_route_cannot_smuggle_unverified_data_or_automatic_promotion() -> None:
    strategy = _read(STRATEGY)
    work = _read(WORK)
    replay = _read(REPLAY)

    for required in (
        "不新增分钟库",
        "不把日线代理写成历史点时验证",
        "不可靠主力资金",
        "DeepSeek 自由文本",
        "不自动重启",
        "用户显式选择",
    ):
        assert required in strategy + work + replay
    assert "v3_explicit_chain_acceptance" in work
    assert "production_authority=false" in work


def test_changelog_records_completed_fail_closed_dataset_and_recall_code() -> None:
    work = _read(DELIVERY_HISTORY)

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
    changelog = _read(DELIVERY_HISTORY)
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
    assert "不新增分钟库" in work


def test_history_training_and_optimization_are_one_dependency_route() -> None:
    work = _read(WORK)
    replay = _read(REPLAY)

    assert "baostock_increment_archive" not in work
    assert "v3_shared_multi_target_samples" in work
    assert "v3_real_full_training" in work
    assert "dynamic_cutoff_and_missing_fact_acquisition" not in work
    assert "初次下载、缺口续传、最近 5 日回读" in replay
    assert "15:00_close" in replay
    assert "point_in_time_parity=false" in replay
    assert "BaoStock 日线允许工程训练，但不能证明运行锚点收益" in replay
    assert "FeatureVectorManifest" in replay
