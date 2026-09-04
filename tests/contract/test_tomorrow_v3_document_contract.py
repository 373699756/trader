from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_daily_close_training_contract_has_non_overlapping_authorities() -> None:
    strategy = " ".join((ROOT / "docs" / "recommendation-strategy.md").read_text(encoding="utf-8").split())

    assert "本节和 第 15.1.36 节定义模型与生产边界" in strategy
    assert "第 15.1.37 节定义唯一所有权" in strategy
    assert "第 15.1.38 节定义日线数据契约" in strategy
    assert not (ROOT / "docs" / "trade.md").exists()
    assert "`trade.md`" not in strategy


def test_v3_is_a_single_offline_industry_model_without_stacking() -> None:
    raw_strategy = (ROOT / "docs" / "recommendation-strategy.md").read_text(encoding="utf-8")
    strategy = " ".join(raw_strategy.split())
    section = raw_strategy[raw_strategy.index("#### 15.1.35") : raw_strategy.index("#### 15.1.36")]
    model_port = (ROOT / "src" / "trader" / "application" / "ports" / "tomorrow_model.py").read_text(encoding="utf-8")

    required_contract = (
        "V3 是新的唯一 Tomorrow 模型",
        "C3 只表示其离线训练阶段",
        "下载数据库中实际存在",
        "训练切分使用下载数据库实际可用的共同完整交易日",
        "先保留最新 200 日",
        "Ridge/LightGBM 50/50",
        "不读取 V1/V2/C3 运行时预测",
        "不做投票或 stacking",
        "`point_in_time_parity=false`",
        "`automatic_model_update=false`",
    )
    assert all(value in strategy for value in required_contract)
    assert "3,000" not in section
    assert "rolling_1500" not in section
    assert 'TomorrowScoringProfile = Literal["v1", "v2", "v3"]' in model_port


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


def test_v3_research_has_four_isolated_owners_and_one_public_command() -> None:
    strategy = (ROOT / "docs" / "recommendation-strategy.md").read_text(encoding="utf-8")
    design = (ROOT / "docs" / "software-business-design.md").read_text(encoding="utf-8")

    required_strategy_contract = (
        "Codex A",
        "Codex B",
        "Codex C",
        "Codex D",
        "#### 15.1.37 V3 四路实施与集成边界",
        "老 V2 predictor、bundle、hash、配置语义、历史和冻结记录全部封存且不修改",
        "./run.sh train-tomorrow",
        "一次命令形成一个由输入 manifest 和 hash 派生的 `run_id`",
        "data/train/tomorrow-v3/<run_id>/",
        "report.json",
        "model.json",
        "evidence/",
        "Codex D",
        "不得自动 promotion",
        "共享文档、CLI",
    )
    assert all(value in strategy for value in required_strategy_contract)
    assert "./run.sh train-tomorrow" in design
    assert "V1/V2/C3 原始预测级联合研究路线" not in design
    assert "内部 V1/V2/C3" not in design
    assert "15.1.36 | `blocked_by_15_1_35`" in strategy

    for internal_stage in (
        "research-tomorrow",
        "research-tomorrow-train",
        "research-tomorrow-confirm",
        "research-tomorrow-holdout",
        "research-tomorrow-promote",
    ):
        assert f"`./run.sh {internal_stage}" not in strategy


def test_trained_v3_profile_remains_hash_bound() -> None:
    strategy = (ROOT / "docs" / "recommendation-strategy.md").read_text(encoding="utf-8")
    model_port = (ROOT / "src" / "trader" / "application" / "ports" / "tomorrow_model.py").read_text(encoding="utf-8")

    assert "data/train/tomorrow-v3/<run_id>/" in strategy
    assert "主程序启动时读取最新 `model.json`" in strategy
    assert "内容 hash" in strategy
    assert 'TomorrowScoringProfile = Literal["v1", "v2", "v3"]' in model_port
