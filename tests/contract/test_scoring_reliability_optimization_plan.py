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
    route = work[work.index("## 4. 历史数据、参数研究、V3 训练与实时评分统一路线") :]

    ordered_tasks = (
        "### 4.1 `outcome_truth_contract`",
        "### 4.2 `feature_contract_unification`",
        "### 4.3 `point_in_time_data_qualification`",
        "### 4.4 `point_in_time_dataset`",
        "### 4.5 `candidate_recall_attribution`",
        "### 4.6 `limited_factor_family_research`",
        "### 4.7 `tomorrow_v3_training_validation`",
        "### 4.8 `incremental_feature_computation`",
        "### 4.9 `risk_cost_uncertainty_deepseek`",
        "### 4.10 `shadow_and_manual_activation`",
    )
    positions = tuple(route.index(task) for task in ordered_tasks)
    assert positions == tuple(sorted(positions))
    assert "历史数据、参数研究、V3 训练与实时评分统一路线" in work
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


def test_work_plan_records_completed_qualification_and_fail_closed_dataset_code() -> None:
    work = _read(WORK)

    assert "当前资格章节：`point_in_time_data_qualification` 已以 `historical_data_insufficient` 完成交付" in work
    assert "`point_in_time_data_qualification`" in work
    assert "`historical_industry_facts` 已以 `historical_data_insufficient` 完成交付" in work
    assert "`point_in_time_dataset` 的失败关闭构建、分片封存和契约代码" in work
    assert "| 4 | `point_in_time_dataset` | `completed: historical_data_insufficient`" in work
    assert "不得重新抓取或修改已经封存的 2000 日日线" in work
    assert "`outcome_truth_contract`" in work
    assert "outcome_price_basis_and_tradability" in work
    assert "v3_single_cost_ownership" in work
    assert "进度在每次开始、发现阻塞、" in work
    assert "完成验证、提交并推送时更新" in work
    assert "D25" in work
    assert "T+2、T+3、T+4、T+5" in work
    assert "#### `baostock_daily_archive`\n\n状态：`completed`" in work


def test_point_in_time_qualification_has_three_independent_gates_and_no_production_authority() -> None:
    strategy = _read(STRATEGY)
    design = _read(DESIGN)
    work = _read(WORK)
    replay = _read(REPLAY)
    combined = strategy + design + work + replay

    for required in (
        "PointInTimeDataQualificationReport",
        "historical_minute_source_qualification",
        "至少 300 只",
        "11:20/14:50",
        "raw/qfq",
        "historical_data_insufficient",
        "production_authority=false",
    ):
        assert required in combined
    assert "5453/5453" in work
    assert "9,085,235" in work
    assert "99.9976%" in work
    assert "分钟点时" in work


def test_history_training_and_optimization_are_one_dependency_route() -> None:
    work = _read(WORK)
    replay = _read(REPLAY)

    assert "历史数据、参数研究、V3 训练与实时评分统一路线" in work
    assert "第 4 节与第 7 节" not in work
    assert "15:00_close" in replay
    assert "point_in_time_parity=false" in replay
    assert "BaoStock 日线不能单独证明 14:50" in replay
    assert "FeatureVectorManifest" in replay
