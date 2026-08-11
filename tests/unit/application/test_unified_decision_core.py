from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from tests.unit.domain.test_decision_identity import NOW, decision
from trader.application.decision_core import UnifiedDecisionIndex
from trader.application.decision_events import V2DecisionCommitted
from trader.domain.recommendation.decision_identity import (
    DecisionOverlay,
    LongProjection,
    LongProjectionItem,
    OverlayQuote,
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
    previous_day = replace(
        decision(sequence=3),
        trade_date=current.trade_date - timedelta(days=1),
        observed_at=NOW - timedelta(days=1),
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
        (OverlayQuote("600001", 10.5, 1.0, "tencent", NOW, "quote-v2"),),
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
        (OverlayQuote("600001", 10.5, 1.0, "tencent", NOW, "quote-v2"),),
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
        (OverlayQuote("600002", 10.0, None, "fixture", NOW, "q-v1"),),
    )

    assert index.publish_overlay(wrong_parent, expected_version=None).reason == "parent_mismatch"
    assert index.publish_overlay(outside, expected_version=None).reason == "quote_scope_mismatch"


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
