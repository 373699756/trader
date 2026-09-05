from dataclasses import replace
from datetime import date, timedelta

import pytest

from trader.domain.research.historical_label import (
    H1CoverageMetadata,
    preregister_historical_label,
    preregister_historical_labels,
)

_HASH_A = "a" * 64
_HASH_B = "b" * 64


def _dates(count: int) -> tuple[date, ...]:
    start = date(2022, 1, 1)
    return tuple(start + timedelta(days=index) for index in range(count))


def _metadata(strategy: str, *, state: str = "coverage_ready", count: int = 1_000) -> H1CoverageMetadata:
    return H1CoverageMetadata(
        strategy=strategy,
        coverage_state=state,
        common_trading_dates=_dates(count) if state == "coverage_ready" else (),
        universe_hash=_HASH_A,
        h1_manifest_hash=_HASH_B,
        source_cutoff=date(2026, 8, 31),
    )


def test_h1_dates_preregister_sixty_twenty_twenty_with_two_five_day_embargoes() -> None:
    result = preregister_historical_label(_metadata("today"))
    split = result.split

    assert result.status == "preregistered"
    assert split is not None
    assert len(split.training_dates) == 595
    assert len(split.first_embargo_dates) == 5
    assert len(split.confirmation_dates) == 195
    assert len(split.second_embargo_dates) == 5
    assert len(split.terminal_holdout_dates) == 200
    assert split.all_dates == _dates(1_000)
    assert split.first_trade_date == _dates(1_000)[0]
    assert split.last_trade_date == _dates(1_000)[-1]
    assert len(split.date_set_hash) == 64
    assert result.terminal_holdout_status == "terminal_holdout_not_opened"
    assert result.candidate_results_generated is False
    assert result.production_authority is False


def test_each_strategy_preregisters_its_fixed_anchor_label_horizons_and_metrics() -> None:
    today, tomorrow, d25 = preregister_historical_labels(
        (_metadata("d25"), _metadata("today"), _metadata("tomorrow"))
    ).strategies

    assert (today.strategy, tomorrow.strategy, d25.strategy) == ("today", "tomorrow", "d25")
    assert today.label.anchor == "11:20"
    assert tomorrow.label.anchor == "14:50"
    assert today.label.horizons == (1,)
    assert tomorrow.label.horizons == (1,)
    assert d25.label.horizons == (2, 3, 4, 5)
    assert d25.label.aggregate == "arithmetic_mean"
    assert today.label.cost_bps == (20, 50, 100)
    assert today.label.gate_cost_bps == (20, 50)
    assert today.label.stress_cost_bps == 100
    assert today.label.benchmark_version == "point_in_time_local_only_equal_weight"
    assert today.label.cash_days_in_denominator is True
    assert today.label.deepseek_history_allowed is False
    assert today.label.parity_dimensions == (
        "trade_date",
        "code",
        "anchor",
        "hard_filter_eligibility",
        "cost",
        "benchmark_market_data",
    )
    assert "moving_block_bootstrap_95_lower_bound" in today.label.required_metrics
    assert "t1_low_mae_atr20" in today.label.required_metrics
    assert "risk_fact_coverage" in tomorrow.label.required_metrics
    assert "four_horizon_net_excess" in d25.label.required_metrics


def test_insufficient_strategy_does_not_block_other_preregistrations() -> None:
    batch = preregister_historical_labels(
        (_metadata("today"), _metadata("tomorrow", state="historical_data_insufficient"), _metadata("d25"))
    )
    assert tuple(item.status for item in batch.strategies) == (
        "preregistered",
        "historical_data_insufficient",
        "preregistered",
    )
    assert batch.strategies[1].split is None
    assert batch.strategies[1].terminal_holdout_status == "terminal_holdout_not_opened"


def test_preregistration_rejects_order_errors_future_dates_and_small_terminal_holdout() -> None:
    metadata = _metadata("today")
    with pytest.raises(ValueError, match="strictly increasing"):
        replace(metadata, common_trading_dates=tuple(reversed(metadata.common_trading_dates)))
    with pytest.raises(ValueError, match="source cutoff"):
        replace(metadata, common_trading_dates=(*metadata.common_trading_dates[:-1], date(2026, 9, 1)))
    result = preregister_historical_label(_metadata("today", count=999))
    assert result.status == "historical_data_insufficient"
    assert result.failure_reasons == ("common_trading_days_below_1000",)


def test_preregistration_identity_is_stable_and_contains_no_return_or_candidate_payload() -> None:
    result = preregister_historical_label(_metadata("tomorrow"))
    replay = preregister_historical_label(_metadata("tomorrow"))

    assert result.content_hash == replay.content_hash
    assert not hasattr(result, "returns")
    assert not hasattr(result, "candidates")
    with pytest.raises(ValueError, match="production authority"):
        replace(result, production_authority=True)
