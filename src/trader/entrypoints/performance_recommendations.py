"""Production recommendation operations for the offline performance runner."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from trader.application.recommendations import PreparedSnapshot, RecommendationEngine
from trader.bootstrap import _recommendation_policy
from trader.domain.market.models import Board, FeatureSnapshot, MarketQuote
from trader.domain.recommendation.models import Strategy
from trader.domain.review.models import DeepSeekReview, DimensionAssessment, ReviewOutcome
from trader.infra.settings import load_runtime_settings, load_strategy_settings


def recommendation_operations(config_path: Path) -> Mapping[str, Callable[[], object]]:
    runtime = load_runtime_settings(config_path)
    settings = load_strategy_settings(runtime.strategy_config_path)
    engine = RecommendationEngine(_recommendation_policy(settings))
    now = datetime(2026, 7, 23, 14, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
    features = tuple(
        _feature(board, index, now) for board in (Board.MAIN, Board.CHINEXT, Board.STAR) for index in range(120)
    )

    def prepare(selected: tuple[FeatureSnapshot, ...] = features) -> PreparedSnapshot:
        return engine.prepare_snapshot(
            Strategy.TOMORROW,
            selected,
            now=now,
            phase="afternoon",
            trade_date=now.date().isoformat(),
            data_version="performance-data-v1",
            review_deadline=now.replace(hour=14, minute=48),
            max_age_seconds=30.0,
            filtered_count=0,
            filter_reasons={},
            market_features=features,
            requested_codes=tuple(feature.quote.code for feature in selected),
            candidate_pool_size=360,
        )

    prepared = prepare()
    applied = _reviews(prepared, now, outcome=ReviewOutcome.APPLIED)
    failed = _reviews(prepared, now, outcome=ReviewOutcome.REJECTED)
    changed = tuple(
        replace(
            feature,
            quote=replace(
                feature.quote,
                price=(feature.quote.price or 0.0) + 0.01,
                data_version="performance-quote-v2",
            ),
        )
        if index < 120
        else feature
        for index, feature in enumerate(features)
    )

    def finalize_substitutes() -> object:
        success = engine.finalize_snapshot(prepared, applied)
        failure = engine.finalize_snapshot(prepared, failed)
        return success, failure

    return {
        "board_ready_to_draft": prepare,
        "quote_to_draft": lambda: prepare(changed),
        "deepseek_to_hybrid": finalize_substitutes,
    }


def _reviews(
    prepared: PreparedSnapshot,
    completed_at: datetime,
    *,
    outcome: ReviewOutcome,
) -> dict[str, DeepSeekReview]:
    names = ("value_quality", "financial_health", "market_flow", "industry_policy", "risk_quality")
    return {
        feature.quote.code: DeepSeekReview(
            code=feature.quote.code,
            outcome=outcome,
            dimensions={
                name: DimensionAssessment(
                    name=name,
                    score=75.0 if outcome is ReviewOutcome.APPLIED else 50.0,
                    confidence=0.8 if outcome is ReviewOutcome.APPLIED else 0.0,
                    assessment="fixed offline substitute",
                )
                for name in names
            },
            risk_facts=(),
            completed_at=completed_at,
            error="" if outcome is ReviewOutcome.APPLIED else "fixed_failure",
            evidence_manifest_hash="offline-performance-manifest",
        )
        for feature in prepared.review_eligible
    }


def _feature(board: Board, index: int, observed_at: datetime) -> FeatureSnapshot:
    prefix = {Board.MAIN: "600", Board.CHINEXT: "300", Board.STAR: "688"}[board]
    code = f"{prefix}{index:03d}"
    quote = MarketQuote(
        code=code,
        name=f"fixture-{code}",
        price=12.0 + index / 100.0,
        previous_close=12.0,
        open_price=12.0,
        high=12.5,
        low=11.8,
        pct_change=2.0,
        change_5m=1.0,
        speed=1.0,
        volume_ratio=2.0,
        turnover_rate=2.5,
        amount=300_000_000.0 + index * 1_000_000.0,
        amplitude=4.0,
        market_cap=30_000_000_000.0,
        industry=f"industry-{index % 10}",
        source="offline-performance-fixture",
        source_time=observed_at,
        received_time=observed_at,
        data_version="performance-data-v1",
        board=board,
        board_source="fixture",
        board_reliability="verified",
        exchange="SZSE" if board is Board.CHINEXT else "SSE",
        listing_date=date(2020, 1, 1),
        listing_age_sessions=1000,
        is_relisted_first_session=False,
        is_delisting_period_first_session=False,
        has_price_limit=True,
        exchange_limit_pct=10.0 if board is Board.MAIN else 20.0,
        strategy_hot_cap_pct=8.0 if board is Board.MAIN else 16.0,
        rule_version="performance-rule-v1",
        rule_effective_date=date(2026, 1, 1),
    )
    values = {
        "amount_median_20d": 200_000_000.0 + index * 1_000_000.0,
        "turnover_median_20d": 1.5,
        "return_1d": 2.0,
        "return_3d": 3.0,
        "return_5d": 5.0,
        "return_10d": 7.0,
        "return_20d": 10.0,
        "return_60d": 15.0,
        "volatility_20d": 2.0,
        "max_drawdown_20d": -8.0,
        "trend_score": 70.0,
        "ma20_60_position": 70.0,
        "ma20_60_structure": 70.0,
        "ma_slope": 70.0,
        "breakout_20d": 70.0,
        "industry_trend": 70.0,
        "tail_return_30m": 1.0,
        "tail_volume_ratio": 1.2,
        "close_location": 70.0,
        "capacity_score": 100.0,
        "moderate_amplitude": 100.0,
        "price_executability": 100.0,
        "limit_distance_safety": 70.0,
        "quality_score": 70.0,
        "value_score": 70.0,
        "growth_score": 70.0,
        "return_20d_not_overheated": 70.0,
        "atr20_pct": 2.0,
        "low_volatility_score": 70.0,
        "low_drawdown_score": 70.0,
        "market_breadth": 60.0,
        "market_regime_score": 50.0,
        "ma5": 12.0,
        "ma10": 11.8,
        "ma20": 11.5,
        "ma20_slope": 0.1,
        "high_20d_previous": 11.9,
    }
    return FeatureSnapshot(quote, values, observed_at, 60, merge_epoch="performance-epoch")


__all__ = ["recommendation_operations"]
