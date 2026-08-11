from __future__ import annotations

from dataclasses import fields, replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from trader.domain.recommendation.decision_identity import (
    CommittedDecisionRecord,
    DecisionItem,
    DecisionOverlay,
    LongProjection,
    LongProjectionItem,
    OverlayQuote,
    ScoredDecision,
)
from trader.domain.recommendation.models import RecommendationAction, Strategy

SHANGHAI = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 8, 11, 14, 40, tzinfo=SHANGHAI)


def decision(
    strategy: Strategy = Strategy.TOMORROW,
    *,
    sequence: int = 1,
    stage: str = "local",
    parent_version: str | None = None,
    score: float = 88.0,
) -> ScoredDecision:
    return ScoredDecision(
        strategy=strategy,
        trade_date=NOW.date(),
        sequence=sequence,
        observed_at=NOW,
        stage=stage,
        parent_version=parent_version,
        input_versions=(("market", "market-v1"), ("daily", "daily-v1")),
        config_version="config-v1",
        strategy_version="strategy-v1",
        fusion_version="fusion-v1",
        items=(
            DecisionItem(
                code="600001",
                action=RecommendationAction.EXECUTABLE,
                selected=True,
                rank=1,
                candidate_score=84.0,
                local_score=score if stage == "local" else 88.0,
                final_score=score,
                score_components=(("trend", 88.0),),
                risk_codes=(),
                reason="selected",
            ),
        ),
        filter_aggregates=(("st_or_delisting", 2),),
    )


def test_scored_identity_is_canonical_for_all_three_scored_strategies() -> None:
    for strategy in (Strategy.TODAY, Strategy.TOMORROW, Strategy.D25):
        first = decision(strategy)
        reordered = replace(first, input_versions=tuple(reversed(first.input_versions)))

        assert reordered.content_hash == first.content_hash
        assert reordered.version == first.version
        assert reordered.strategy is strategy


def test_scored_identity_rejects_long_and_invalid_hybrid_parent_or_local_score() -> None:
    with pytest.raises(ValueError, match="scored strategy"):
        decision(Strategy.LONG)
    with pytest.raises(ValueError, match="hybrid.*parent"):
        decision(stage="hybrid")
    with pytest.raises(ValueError, match="local decision"):
        current = decision()
        replace(current, items=(replace(current.items[0], final_score=87.99999999),))


def test_long_projection_has_no_scoring_fields_and_stable_identity() -> None:
    projection = LongProjection(
        trade_date=NOW.date(),
        sequence=1,
        observed_at=NOW,
        input_versions=(("quotes", "quotes-v1"),),
        items=(
            LongProjectionItem(
                "600001",
                "group:001",
                "quote-v1",
                name="甲公司",
                industry="设备",
                price=10.5,
                pct_change=1.2,
                amount=100_000_000.0,
                turnover_rate=2.0,
                market_cap=10_000_000_000.0,
                source="tencent",
                source_time=NOW,
                quote_status="live",
            ),
        ),
    )

    assert projection.strategy is Strategy.LONG
    assert "score" not in {item.name for item in fields(LongProjectionItem)}
    assert projection.items[0].price == 10.5
    assert projection.items[0].quote_status == "live"
    assert projection.version.startswith("projection:long:")


def test_long_projection_preserves_watchlist_order_and_allows_missing_placeholders() -> None:
    projection = LongProjection(
        trade_date=NOW.date(),
        sequence=1,
        observed_at=NOW,
        input_versions=(("watchlist", "watchlist-v1"),),
        items=(
            LongProjectionItem("600002", "group:002", "missing:watchlist-v1"),
            LongProjectionItem("600001", "group:001", "missing:watchlist-v1"),
        ),
    )

    assert tuple(item.code for item in projection.items) == ("600002", "600001")
    assert all(item.quote_status == "missing" and item.price is None for item in projection.items)


def test_overlay_and_formal_record_validate_parent_time_scope_and_hash() -> None:
    current = decision()
    quote = OverlayQuote("600001", 10.5, 1.2, "tencent", NOW, "quote-v2")
    overlay = DecisionOverlay(
        strategy=Strategy.TOMORROW,
        trade_date=NOW.date(),
        parent_version=current.version,
        observed_at=NOW,
        quotes=(quote,),
    )
    record = CommittedDecisionRecord(current, NOW + timedelta(minutes=10), "scheduled")

    assert overlay.version.startswith("overlay:tomorrow:")
    assert len(record.payload_hash) == 64
    with pytest.raises(ValueError, match="future quote"):
        replace(overlay, quotes=(replace(quote, source_time=NOW + timedelta(seconds=1)),))
    with pytest.raises(ValueError, match="trade date"):
        replace(record, committed_at=NOW + timedelta(days=1))
