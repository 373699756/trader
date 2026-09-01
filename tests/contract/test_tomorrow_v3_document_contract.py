from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_daily_close_training_contract_has_one_authoritative_owner() -> None:
    strategy = (ROOT / "docs" / "recommendation-strategy.md").read_text(encoding="utf-8")
    trade_index = (ROOT / "docs" / "trade.md").read_text(encoding="utf-8")

    assert "本节及第 15.1.36 节是日线训练、联合评分、工件、命令和本机资源方案的" in strategy
    assert "唯一权威" in strategy
    assert "本文件不再维护第二套" in trade_index
    assert "第 15.1.35–15.1.37 节" in trade_index
    assert "recommendation-strategy.md#15135-tomorrow-日线收盘代理训练分支" in trade_index


def test_v3_fuses_raw_predictions_without_activating_a_production_profile() -> None:
    strategy = (ROOT / "docs" / "recommendation-strategy.md").read_text(encoding="utf-8")
    model_port = (ROOT / "src" / "trader" / "application" / "ports" / "tomorrow_model.py").read_text(encoding="utf-8")

    required_contract = (
        "V1/V2/C3 同批原始预测",
        "联合权重必须非负且总和为 1",
        "允许 V2 权重精确收缩为 0",
        "然后且只映射一次 `base_score`",
        "最新至少 200 个共同有效交易日必须",
        "永久保留给第 15.1.32 节的 14:50 点时终端留出",
        "过滤证据不完整的股票仍不进入 V3 推理",
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
        "./run.sh research-tomorrow",
        "每次调用只推进一个",
        "不得自动 promotion",
        "共享文件",
    )
    assert all(value in strategy for value in required_strategy_contract)
    assert "./run.sh research-tomorrow" in design

    for internal_stage in (
        "research-tomorrow-train",
        "research-tomorrow-confirm",
        "research-tomorrow-holdout",
        "research-tomorrow-promote",
    ):
        assert f"`./run.sh {internal_stage}" not in strategy
