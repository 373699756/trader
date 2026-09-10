from __future__ import annotations

from pathlib import Path

from trader.application.research.tomorrow_historical_validation import HISTORICAL_RISK_VALIDATION_SPEC

ROOT = Path(__file__).resolve().parents[2]
STRATEGY = ROOT / "docs/01_评分逻辑.md"


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
        "报告必须绑定规范、active snapshot、模型/候选和证据 hash",
        "同内容重放幂等",
        "不同内容冲突",
        "production_authority=false",
        "不授权后台自动更新",
    ):
        assert token in strategy


def test_historical_risk_probability_gate_is_fixed_before_production_use() -> None:
    strategy = " ".join(STRATEGY.read_text(encoding="utf-8").split())

    for token in (
        "MAE/ATR20 <= -1.5",
        "loss_probability_status=not_modeled",
    ):
        assert token in strategy
    assert HISTORICAL_RISK_VALIDATION_SPEC.research_identity == "tomorrow_historical_risk_probability"
    assert (
        HISTORICAL_RISK_VALIDATION_SPEC.training_trade_dates,
        HISTORICAL_RISK_VALIDATION_SPEC.calibration_trade_dates,
        HISTORICAL_RISK_VALIDATION_SPEC.test_trade_dates,
    ) == (60, 20, 40)
    assert HISTORICAL_RISK_VALIDATION_SPEC.maximum_expected_calibration_error == 0.05
