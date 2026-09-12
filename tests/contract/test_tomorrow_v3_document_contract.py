from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_daily_close_training_contract_has_non_overlapping_authorities() -> None:
    work = " ".join((ROOT / "docs" / "03_工程实施.md").read_text(encoding="utf-8").split())

    assert "v3_training_artifact_rebuild" in work
    assert "terminal_holdout_and_shadow" in work
    assert "manual_candidate_strategy_activation" in work
    assert "15.1.36" not in work
    assert "15.1.37" not in work
    assert "15.1.38" not in work
    assert not (ROOT / "docs" / "trade.md").exists()
    assert "`trade.md`" not in work


def test_v3_is_a_single_offline_industry_model_without_stacking() -> None:
    strategy = (ROOT / "docs" / "01_评分逻辑.md").read_text(encoding="utf-8")
    design = (ROOT / "docs" / "02_工程设计.md").read_text(encoding="utf-8")
    replay = (ROOT / "docs" / "04_策略回溯.md").read_text(encoding="utf-8")
    plan = (ROOT / "docs" / "03_工程实施.md").read_text(encoding="utf-8")
    work = " ".join((strategy + design + replay + plan).split())
    required_contract = (
        "V3",
        "Tomorrow",
        "Ridge/LightGBM",
        "V3 三策略 stacking 不是当前路线",
        "point_in_time_parity=false",
        "automatic_model_update=false",
    )
    assert all(value in work for value in required_contract)
    assert 'ScoringProfileId = Literal["v1", "v2", "v3"]' in (
        ROOT / "src" / "trader" / "domain" / "recommendation" / "model_scoring" / "profile_identity.py"
    ).read_text(encoding="utf-8")


def test_v3_minimum_dates_can_satisfy_every_preregistered_segment() -> None:
    minimum_dates = 1_250
    point_in_time_reserve = 200
    daily_close_dates = minimum_dates - point_in_time_reserve
    first_boundary = daily_close_dates * 60 // 100
    second_boundary = daily_close_dates * 80 // 100

    development_dates = first_boundary - 5
    confirmation_dates = second_boundary - first_boundary - 5
    daily_close_holdout_dates = daily_close_dates - second_boundary

    assert development_dates >= 600
    assert confirmation_dates >= 200
    assert daily_close_holdout_dates >= 200


def test_remaining_v3_research_has_isolated_owners_and_one_public_command() -> None:
    strategy = (ROOT / "docs" / "03_工程实施.md").read_text(encoding="utf-8")
    design = (ROOT / "docs" / "02_工程设计.md").read_text(encoding="utf-8")

    required_strategy_contract = (
        "v3_training_artifact_rebuild",
        "v3_runtime_acceptance",
        "terminal_holdout_and_shadow",
        "manual_candidate_strategy_activation",
        "report.json",
        "model.json",
        "training-input.json",
        "默认 V1 不变",
    )
    assert all(value in strategy for value in required_strategy_contract)
    assert "dynamic_cutoff_and_missing_fact_acquisition" not in strategy
    assert "./run.sh train-tomorrow" in design
    assert "V1/V2/C3 原始预测级联合研究路线" not in design
    assert "内部 V1/V2/C3" not in design
    assert "15.1.36 V3 条件式生产适配 | `blocked_by_15.1.35`" not in strategy
    assert "状态：`blocked_by_candidate_validation`" in strategy
    assert "状态：`blocked_by_shadow_and_user_authorization`" in strategy

    for internal_stage in (
        "research-tomorrow",
        "research-tomorrow-train",
        "research-tomorrow-confirm",
        "research-tomorrow-holdout",
        "research-tomorrow-promote",
    ):
        assert f"`./run.sh {internal_stage}" not in strategy


def test_trained_v3_profile_loads_only_the_fixed_active_bundle_files() -> None:
    strategy = (ROOT / "docs" / "03_工程实施.md").read_text(encoding="utf-8")
    design = (ROOT / "docs" / "02_工程设计.md").read_text(encoding="utf-8")
    model_port = (ROOT / "src" / "trader" / "application" / "ports" / "model_scoring.py").read_text(encoding="utf-8")

    locator = (ROOT / "src/trader/infra/scoring/profiles/v3/bundle_locator.py").read_text(encoding="utf-8")
    bundle_repository_source = (ROOT / "src/trader/infra/scoring/profiles/v3/training_bundle_repository.py").read_text(
        encoding="utf-8"
    )
    assert "tomorrow-v3" in locator
    assert "active-bundle.json" in bundle_repository_source
    assert "model.json" in bundle_repository_source
    assert "training-input.json" in bundle_repository_source
    assert "report.json" in bundle_repository_source
    assert "generations" not in locator + bundle_repository_source + design
    assert "active snapshot 和来源身份全部有效" in strategy
    assert "active-bundle.json" in design
    assert "四个固定文件" in design
    assert "15:00_close_proxy" in design
    assert "class ModelPredictorPort" in model_port


def test_v3_training_owns_one_active_archive_and_disk_backed_sample_source() -> None:
    training = (ROOT / "src/trader/infra/scoring/profiles/v3/training.py").read_text(encoding="utf-8")
    sample_builder = (ROOT / "src/trader/infra/scoring/profiles/v3/sample_builder.py").read_text(encoding="utf-8")

    assert "SQLiteHistoryTrainingInputArchive.open" in training
    assert "BaoStockTrainingTrainingInputArchive" not in training
    assert "SQLiteTomorrowTrainingSampleRepository" in training
    assert "read_training_batch" not in training
    assert "iter_training_windows" in sample_builder
    assert "read_training_batch" not in sample_builder
    assert "defaultdict" not in training
    assert "tuple[_Sample" not in training
    assert "allow_partial_history" not in training
