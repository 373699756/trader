from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STRATEGY = ROOT / "docs/recommendation-strategy.md"


def test_score_research_uses_ordered_historical_splits_and_bounded_terminal_states() -> None:
    strategy = " ".join(STRATEGY.read_text(encoding="utf-8").split())

    for token in (
        "按交易日排序",
        "禁止随机拆分",
        "embargo",
        "historical_data_insufficient",
        "historical_rejected",
        "historical_validated",
        "合法空仓日",
    ):
        assert token in strategy


def test_score_research_keeps_runtime_outcomes_out_of_validation() -> None:
    strategy = " ".join(STRATEGY.read_text(encoding="utf-8").split())

    assert "线上 T+1 结算只用于正式推荐历史与运行监控" in strategy
    assert "不进入评分训练、校准、历史门禁、自动调参或生产切换" in strategy


def test_historical_reports_are_tamper_evident_and_non_production() -> None:
    strategy = " ".join(STRATEGY.read_text(encoding="utf-8").split())

    for token in (
        "报告必须绑定规范、父归档、manifest、模型/候选和证据 hash",
        "同内容重放幂等",
        "不同内容冲突",
        "production_authority=false",
        "不授权后台自动更新",
    ):
        assert token in strategy


def test_historical_risk_probability_gate_is_fixed_before_production_use() -> None:
    strategy = " ".join(STRATEGY.read_text(encoding="utf-8").split())

    for token in (
        "tomorrow_v2_historical_risk_probability_v1",
        "MAE / ATR20 <= -1.5",
        "60 日训练、20 日校准、40 日独立检验",
        "Brier 分数严格优于",
        "ECE 不超过 0.05",
        "loss_probability_status=not_modeled",
    ):
        assert token in strategy
