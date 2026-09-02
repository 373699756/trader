from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_daily_close_training_contract_has_one_authoritative_owner() -> None:
    strategy = (ROOT / "docs" / "recommendation-strategy.md").read_text(encoding="utf-8")

    assert "本节与第 15.1.36 节是训练、工件、命令和资源方案的唯一权威" in strategy
    assert "资源方案的唯一权威" in strategy
    assert not (ROOT / "docs" / "trade.md").exists()
    assert "`trade.md`" not in strategy


def test_v3_is_a_single_offline_industry_model_without_stacking() -> None:
    strategy = (ROOT / "docs" / "recommendation-strategy.md").read_text(encoding="utf-8")
    model_port = (ROOT / "src" / "trader" / "application" / "ports" / "tomorrow_model.py").read_text(encoding="utf-8")

    required_contract = (
        "V3 是新的唯一 Tomorrow 模型",
        "C3 只表示其离线训练阶段",
        "Ridge/LightGBM 50/50",
        "不读取 V1/V2/C3 运行时预测",
        "不做投票或 stacking",
        "最新至少 200 个共同有效交易日保留",
        "`point_in_time_parity=false`",
        "`automatic_model_update=false`",
    )
    assert all(value in strategy for value in required_contract)
    assert 'TomorrowScoringProfile = Literal["v1", "v2"]' in model_port
    assert 'Literal["v1", "v2", "v3"]' not in model_port


def test_all_unfinished_research_has_four_isolated_owners_and_one_public_command() -> None:
    strategy = (ROOT / "docs" / "recommendation-strategy.md").read_text(encoding="utf-8")
    design = (ROOT / "docs" / "software-business-design.md").read_text(encoding="utf-8")

    required_strategy_contract = (
        "Codex A",
        "Codex B",
        "Codex C",
        "Codex D",
        "15.1.25–15.1.36",
        "V1/V2 工程任务已完成并冻结",
        "./run.sh train-tomorrow",
        "一次调用连续完成",
        ".runtime/v2/research/tomorrow-v3/<run_id>/",
        "report.json",
        "model.json",
        "evidence/",
        "Codex D",
        "不得自动 promotion",
        "共享文件",
    )
    assert all(value in strategy for value in required_strategy_contract)
    assert "./run.sh train-tomorrow" in design

    for internal_stage in (
        "research-tomorrow",
        "research-tomorrow-train",
        "research-tomorrow-confirm",
        "research-tomorrow-holdout",
        "research-tomorrow-promote",
    ):
        assert f"`./run.sh {internal_stage}" not in strategy


def test_implemented_training_command_does_not_pre_authorize_v3() -> None:
    strategy = (ROOT / "docs" / "recommendation-strategy.md").read_text(encoding="utf-8")
    model_port = (ROOT / "src" / "trader" / "application" / "ports" / "tomorrow_model.py").read_text(encoding="utf-8")

    assert "独立高风险批次" in strategy
    assert "独立高风险发布批次" in strategy
    assert 'TomorrowScoringProfile = Literal["v1", "v2"]' in model_port
    assert 'Literal["v1", "v2", "v3"]' not in model_port
