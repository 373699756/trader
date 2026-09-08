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
        "outcome_truth_contract",
        "feature_contract_unification",
        "incremental_feature_computation",
        "point_in_time_data_qualification",
        "candidate_recall_attribution",
        "limited_factor_family_research",
        "risk_cost_uncertainty_deepseek",
        "shadow_and_manual_activation",
    )
    positions = tuple(work.index(task) for task in ordered_tasks)
    assert positions == tuple(sorted(positions))
    assert "评分链路可靠性与收益优化计划" in work
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
    assert "scoring-feature-outcome-optimization" in work


def test_work_plan_records_current_chapter_and_progress_update_contract() -> None:
    work = _read(WORK)

    assert "当前执行章节：`outcome_truth_contract`" in work
    assert "每次开始、发现阻塞、完成验证、提交并推送" in work
    assert "D25 T+2/T+3/T+4/T+5" in work
    assert "`baostock_daily_archive` 保持 `pending`" in work
