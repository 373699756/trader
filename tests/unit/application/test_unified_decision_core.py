from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from tests.unit.domain.test_decision_identity import NOW, decision
from trader.application.decisions.decision_core import UnifiedDecisionIndex
from trader.application.decisions.decision_events import V2DecisionCommitted
from trader.domain.recommendation.decision_identity import (
    DecisionOverlay,
    DecisionQuote,
    LongProjection,
    LongProjectionItem,
)
from trader.domain.recommendation.models import Strategy


def test_unified_index_allows_one_concurrent_expected_version_winner() -> None:
    index = UnifiedDecisionIndex()
    first = decision(sequence=1)
    assert index.publish(first, expected_version=None).accepted
    candidates = (
        decision(sequence=2, score=88.0),
        decision(sequence=2, score=89.0),
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(
            executor.map(
                lambda item: index.publish(item, expected_version=first.version),
                candidates,
            )
        )

    assert sum(item.accepted for item in results) == 1
    assert {item.reason for item in results} == {"accepted", "cas_mismatch"}
    event = next(item.event for item in results if item.accepted)
    assert isinstance(event, V2DecisionCommitted)
    assert event.items[0].score_components == (("trend", 88.0),)


def test_hybrid_requires_the_current_same_strategy_local_parent() -> None:
    index = UnifiedDecisionIndex()
    local = decision(Strategy.TOMORROW)
    index.publish(local, expected_version=None)
    wrong = decision(
        Strategy.TOMORROW,
        sequence=2,
        stage="hybrid",
        parent_version="decision:wrong",
        score=90.0,
    )
    hybrid = replace(wrong, parent_version=local.version)

    assert index.publish(wrong, expected_version=local.version).reason == "parent_mismatch"
    assert index.publish(hybrid, expected_version=local.version).accepted


def test_index_rejects_late_sequence_date_and_cross_date_hybrid_parent() -> None:
    index = UnifiedDecisionIndex()
    current = decision(sequence=2)
    assert index.publish(current, expected_version=None).accepted
    stale_sequence = decision(sequence=1)
    previous_item = decision(sequence=3).items[0]
    assert previous_item.quote is not None
    previous_day = replace(
        decision(sequence=3),
        trade_date=current.trade_date - timedelta(days=1),
        observed_at=NOW - timedelta(days=1),
        items=(replace(previous_item, quote=replace(previous_item.quote, source_time=NOW - timedelta(days=1))),),
    )
    next_day_hybrid = replace(
        decision(sequence=3, stage="hybrid", parent_version=current.version),
        trade_date=current.trade_date + timedelta(days=1),
        observed_at=NOW + timedelta(days=1),
    )

    assert index.publish(stale_sequence, expected_version=current.version).reason == "stale_sequence"
    assert index.publish(previous_day, expected_version=current.version).reason == "stale_trade_date"
    assert index.publish(next_day_hybrid, expected_version=current.version).reason == "parent_mismatch"


def test_strategies_and_overlays_are_cas_isolated() -> None:
    index = UnifiedDecisionIndex()
    tomorrow = decision(Strategy.TOMORROW)
    today = decision(Strategy.TODAY)
    assert index.publish(tomorrow, expected_version=None).accepted
    assert index.publish(today, expected_version=None).accepted
    overlay = DecisionOverlay(
        Strategy.TOMORROW,
        tomorrow.trade_date,
        tomorrow.version,
        NOW,
        (_quote(),),
    )

    assert index.publish_overlay(overlay, expected_version=None).accepted
    assert index.snapshot(Strategy.TODAY).current == today
    assert index.snapshot(Strategy.TODAY).overlay is None
    assert index.snapshot(Strategy.TOMORROW).overlay == overlay
    assert index.publish_overlay(overlay, expected_version=None).reason == "overlay_cas_mismatch"


def test_long_projection_uses_the_same_cas_and_overlay_without_a_scored_event() -> None:
    index = UnifiedDecisionIndex()
    projection = LongProjection(
        trade_date=NOW.date(),
        sequence=1,
        observed_at=NOW,
        input_versions=(("quotes", "quotes-v1"),),
        items=(LongProjectionItem("600001", "core", "quote-v1"),),
    )
    published = index.publish(projection, expected_version=None)
    overlay = DecisionOverlay(
        Strategy.LONG,
        projection.trade_date,
        projection.version,
        NOW,
        (_quote(),),
    )

    assert published.accepted
    assert published.event is None
    assert index.publish_overlay(overlay, expected_version=None).accepted
    assert index.snapshot(Strategy.LONG).overlay == overlay


def test_overlay_rejects_wrong_parent_and_out_of_scope_code() -> None:
    index = UnifiedDecisionIndex()
    current = decision()
    index.publish(current, expected_version=None)
    wrong_parent = DecisionOverlay(Strategy.TOMORROW, current.trade_date, "wrong", NOW, ())
    outside = DecisionOverlay(
        Strategy.TOMORROW,
        current.trade_date,
        current.version,
        NOW,
        (replace(_quote(), code="600002"),),
    )

    assert index.publish_overlay(wrong_parent, expected_version=None).reason == "parent_mismatch"
    assert index.publish_overlay(outside, expected_version=None).reason == "quote_scope_mismatch"


def test_scored_identity_and_complete_initial_overlay_publish_atomically() -> None:
    index = UnifiedDecisionIndex()
    local = decision()
    anchor = local.items[0].quote
    assert anchor is not None
    overlay = DecisionOverlay(
        local.strategy,
        local.trade_date,
        local.version,
        NOW,
        (anchor,),
    )

    published = index.publish_scored(local, overlay, expected_version=None)

    assert published.accepted
    snapshot = index.snapshot(local.strategy)
    assert snapshot.current == local
    assert snapshot.overlay == overlay
    assert snapshot.overlay.quotes[0].amount == 120_000_000.0
    assert snapshot.overlay.quotes[0].turnover_rate == 2.1
    assert snapshot.overlay.quotes[0].market_cap == 12_000_000_000.0


def test_invalid_initial_overlay_leaves_previous_identity_and_overlay_unchanged() -> None:
    index = UnifiedDecisionIndex()
    local = decision()
    anchor = local.items[0].quote
    assert anchor is not None
    overlay = DecisionOverlay(local.strategy, local.trade_date, local.version, NOW, (anchor,))
    assert index.publish_scored(local, overlay, expected_version=None).accepted
    before = index.snapshot(local.strategy)
    next_local = decision(sequence=3, score=90.0)
    next_anchor = next_local.items[0].quote
    assert next_anchor is not None
    invalid = DecisionOverlay(next_local.strategy, next_local.trade_date, next_local.version, NOW, ())

    result = index.publish_scored(next_local, invalid, expected_version=local.version)

    assert result.reason == "quote_scope_mismatch"
    assert index.snapshot(local.strategy) == before

    mismatched = DecisionOverlay(
        next_local.strategy,
        next_local.trade_date,
        next_local.version,
        NOW,
        (replace(next_anchor, price=10.6),),
    )
    result = index.publish_scored(next_local, mismatched, expected_version=local.version)

    assert result.reason == "quote_identity_mismatch"
    assert index.snapshot(local.strategy) == before


def _quote() -> DecisionQuote:
    return DecisionQuote(
        "600001",
        10.5,
        1.0,
        120_000_000.0,
        2.1,
        12_000_000_000.0,
        "tencent",
        NOW,
        "quote-v2",
    )


def test_freeze_seal_projects_official_items_and_rejects_all_same_day_updates() -> None:
    index = UnifiedDecisionIndex()
    local = decision()
    assert index.publish(local, expected_version=None).accepted
    boundary = datetime.combine(local.trade_date, datetime.min.time(), tzinfo=ZoneInfo("Asia/Shanghai")).replace(
        hour=14,
        minute=50,
    )

    sealed = index.seal_for_freeze(Strategy.TOMORROW, boundary_at=boundary)
    retry = index.seal_for_freeze(Strategy.TOMORROW, boundary_at=boundary)

    assert sealed.accepted and sealed.decision is not None
    assert retry.decision == sealed.decision
    assert index.publish(decision(sequence=3), expected_version=local.version).reason == "freeze_sealed"
