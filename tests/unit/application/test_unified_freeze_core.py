from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta
import threading
from zoneinfo import ZoneInfo

from tests.unit.domain.test_freeze_identity import NOW, freeze
from trader.recommendation.application.pipeline.freeze_publish.freeze_events import FreezeCommitted
from trader.recommendation.application.pipeline.freeze_publish.snapshot_publisher import (
    UnifiedFreezeIndex,
    UnifiedProfilePair,
)
from trader.recommendation.domain.publication.freeze_identity import (
    DisplayQuoteCodes,
    FreezeOverlay,
    FreezeQuote,
    LongProjection,
    LongProjectionItem,
)
from trader.recommendation.domain.publication.models import RecommendationAction, Strategy


def test_unified_index_allows_one_concurrent_expected_version_winner() -> None:
    index = UnifiedFreezeIndex()
    first = freeze(sequence=1)
    assert index.publish(first, expected_version=None).accepted
    candidates = (
        freeze(sequence=2, score=88.0),
        freeze(sequence=2, score=89.0),
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
    assert isinstance(event, FreezeCommitted)
    assert event.items[0].score_components == (("trend", 88.0),)


def test_profiled_batch_rejects_every_profile_before_any_partial_write() -> None:
    lock = threading.RLock()
    indexes = (UnifiedFreezeIndex(transaction_lock=lock), UnifiedFreezeIndex(transaction_lock=lock))
    pairs = []
    for profile, index, offset in (("v1", indexes[0], 0), ("v2", indexes[1], 2)):
        freezes = (
            freeze(Strategy.TOMORROW, sequence=1 + offset),
            freeze(Strategy.D25, sequence=2 + offset),
        )
        overlays = tuple(
            FreezeOverlay(
                item.strategy,
                item.trade_date,
                item.version,
                item.observed_at,
                (item.items[0].quote,),
                sequence=1,
            )
            for item in freezes
        )
        pairs.append(UnifiedProfilePair(profile, index, freezes, overlays, (None, None)))

    rejected = replace(pairs[1], expected_versions=("missing", None))
    result = UnifiedFreezeIndex.publish_scored_batch((pairs[0], rejected))

    assert not result.accepted and result.reason == "cas_mismatch"
    assert all(index.snapshot(strategy).current is None for index in indexes for strategy in (Strategy.TOMORROW, Strategy.D25))
    expired = UnifiedFreezeIndex.publish_scored_batch((pairs[0], pairs[1]), lambda: False)
    assert not expired.accepted and expired.reason == "round_timeout"
    assert all(index.snapshot(strategy).current is None for index in indexes for strategy in (Strategy.TOMORROW, Strategy.D25))
    accepted = UnifiedFreezeIndex.publish_scored_batch((pairs[0], pairs[1]))
    assert accepted.accepted
    assert tuple(profile for profile, _events in accepted.events) == ("v1", "v2")
    captured = UnifiedFreezeIndex.snapshot_batch(indexes)
    assert tuple(tuple(snapshot.current for snapshot in pair) for pair in captured) == tuple(pair.freezes for pair in pairs)
    with ThreadPoolExecutor(max_workers=1) as executor:
        with lock:
            reader = executor.submit(UnifiedFreezeIndex.snapshot_batch, indexes)
            assert not reader.done()
        assert reader.result(timeout=2) == captured


def test_hybrid_requires_the_current_same_strategy_local_parent() -> None:
    index = UnifiedFreezeIndex()
    local = freeze(Strategy.TOMORROW)
    index.publish(local, expected_version=None)
    wrong = freeze(
        Strategy.TOMORROW,
        sequence=2,
        stage="hybrid",
        parent_version="freeze:wrong",
        score=90.0,
    )
    hybrid = replace(wrong, parent_version=local.version)

    assert index.publish(wrong, expected_version=local.version).reason == "parent_mismatch"
    assert index.publish(hybrid, expected_version=local.version).accepted


def test_index_rejects_late_sequence_date_and_cross_date_hybrid_parent() -> None:
    index = UnifiedFreezeIndex()
    current = freeze(sequence=2)
    assert index.publish(current, expected_version=None).accepted
    stale_sequence = freeze(sequence=1)
    previous_item = freeze(sequence=3).items[0]
    assert previous_item.quote is not None
    previous_day = replace(
        freeze(sequence=3),
        trade_date=current.trade_date - timedelta(days=1),
        observed_at=NOW - timedelta(days=1),
        items=(replace(previous_item, quote=replace(previous_item.quote, source_time=NOW - timedelta(days=1))),),
    )
    next_day_hybrid = replace(
        freeze(sequence=3, stage="hybrid", parent_version=current.version),
        trade_date=current.trade_date + timedelta(days=1),
        observed_at=NOW + timedelta(days=1),
    )

    assert index.publish(stale_sequence, expected_version=current.version).reason == "stale_sequence"
    assert index.publish(previous_day, expected_version=current.version).reason == "stale_trade_date"
    assert index.publish(next_day_hybrid, expected_version=current.version).reason == "parent_mismatch"


def test_strategies_and_overlays_are_cas_isolated() -> None:
    index = UnifiedFreezeIndex()
    tomorrow = freeze(Strategy.TOMORROW)
    d25 = freeze(Strategy.D25)
    assert index.publish(tomorrow, expected_version=None).accepted
    assert index.publish(d25, expected_version=None).accepted
    overlay = FreezeOverlay(Strategy.TOMORROW, tomorrow.trade_date, tomorrow.version, NOW, (_quote(),), sequence=1)

    assert index.publish_overlay(overlay, expected_version=None).accepted
    assert index.snapshot(Strategy.D25).current == d25
    assert index.snapshot(Strategy.D25).overlay is None
    assert index.snapshot(Strategy.TOMORROW).overlay == overlay
    assert index.publish_overlay(overlay, expected_version=None).reason == "overlay_cas_mismatch"


def test_long_projection_uses_the_same_cas_and_overlay_without_a_scored_event() -> None:
    index = UnifiedFreezeIndex()
    projection = LongProjection(
        trade_date=NOW.date(),
        sequence=1,
        observed_at=NOW,
        input_versions=(("quotes", "quotes-fixture"),),
        items=(LongProjectionItem("600001", "core", "quote-fixture"),),
    )
    published = index.publish(projection, expected_version=None)
    overlay = FreezeOverlay(Strategy.LONG, projection.trade_date, projection.version, NOW, (_quote(),), sequence=1)

    assert published.accepted
    assert published.event is None
    assert index.publish_overlay(overlay, expected_version=None).accepted
    assert index.snapshot(Strategy.LONG).overlay == overlay


def test_overlay_rejects_wrong_parent_and_out_of_scope_code() -> None:
    index = UnifiedFreezeIndex()
    current = freeze()
    index.publish(current, expected_version=None)
    wrong_parent = FreezeOverlay(Strategy.TOMORROW, current.trade_date, "wrong", NOW, (), sequence=1)
    outside = FreezeOverlay(
        Strategy.TOMORROW, current.trade_date, current.version, NOW, (replace(_quote(), code="600002"),), sequence=1
    )

    assert index.publish_overlay(wrong_parent, expected_version=None).reason == "parent_mismatch"
    assert index.publish_overlay(outside, expected_version=None).reason == "quote_scope_mismatch"


def test_scored_identity_and_complete_initial_overlay_publish_atomically() -> None:
    index = UnifiedFreezeIndex()
    local = freeze()
    anchor = local.items[0].quote
    assert anchor is not None
    overlay = FreezeOverlay(local.strategy, local.trade_date, local.version, NOW, (anchor,), sequence=1)

    published = index.publish_scored(local, overlay, expected_version=None)

    assert published.accepted
    snapshot = index.snapshot(local.strategy)
    assert snapshot.current == local
    assert snapshot.overlay == overlay
    assert snapshot.overlay.quotes[0].amount == 120_000_000.0
    assert snapshot.overlay.quotes[0].turnover_rate == 2.1
    assert snapshot.overlay.quotes[0].market_cap == 12_000_000_000.0


def test_invalid_initial_overlay_leaves_previous_identity_and_overlay_unchanged() -> None:
    index = UnifiedFreezeIndex()
    local = freeze()
    anchor = local.items[0].quote
    assert anchor is not None
    overlay = FreezeOverlay(local.strategy, local.trade_date, local.version, NOW, (anchor,), sequence=1)
    assert index.publish_scored(local, overlay, expected_version=None).accepted
    before = index.snapshot(local.strategy)
    next_local = freeze(sequence=3, score=90.0)
    next_anchor = next_local.items[0].quote
    assert next_anchor is not None
    invalid = FreezeOverlay(next_local.strategy, next_local.trade_date, next_local.version, NOW, (), sequence=1)

    result = index.publish_scored(next_local, invalid, expected_version=local.version)

    assert result.reason == "quote_scope_mismatch"
    assert index.snapshot(local.strategy) == before

    mismatched = FreezeOverlay(
        next_local.strategy,
        next_local.trade_date,
        next_local.version,
        NOW,
        (replace(next_anchor, price=10.6),),
        sequence=1,
    )
    result = index.publish_scored(next_local, mismatched, expected_version=local.version)

    assert result.reason == "quote_identity_mismatch"
    assert index.snapshot(local.strategy) == before


def test_paired_scored_publish_changes_both_strategies_or_neither() -> None:
    index = UnifiedFreezeIndex()
    first = (freeze(Strategy.TOMORROW), freeze(Strategy.D25))
    first_overlays = tuple(
        FreezeOverlay(item.strategy, item.trade_date, item.version, NOW, (item.items[0].quote,), sequence=1)
        for item in first
    )
    initial = index.publish_scored_pair(first, first_overlays, (None, None))
    assert initial.accepted and len(initial.events) == 2
    assert all(index.snapshot(item.strategy).current == item for item in first)

    next_pair = (freeze(Strategy.TOMORROW, sequence=3), freeze(Strategy.D25, sequence=3))
    next_overlays = tuple(
        FreezeOverlay(item.strategy, item.trade_date, item.version, NOW, (item.items[0].quote,), sequence=2)
        for item in next_pair
    )
    rejected = index.publish_scored_pair(next_pair, next_overlays, (first[0].version, "wrong-version"))
    assert not rejected.accepted and rejected.reason == "cas_mismatch"
    assert all(index.snapshot(item.strategy).current == item for item in first)

    accepted = index.publish_scored_pair(next_pair, next_overlays, (first[0].version, first[1].version))
    assert accepted.accepted and len(accepted.events) == 2
    assert all(index.snapshot(item.strategy).current == item for item in next_pair)


def test_fixed_avoid_seat_quote_can_refresh_without_changing_score() -> None:
    index = UnifiedFreezeIndex()
    initial = freeze()
    extra = replace(
        initial.items[0],
        code="600002",
        selected=True,
        rank=2,
        selection_rank=2,
        action=RecommendationAction.UNAVAILABLE,
        quote=replace(initial.items[0].quote, code="600002"),
    )
    current = replace(initial, items=(*initial.items, extra))
    assert DisplayQuoteCodes(current) == {"600001", "600002"}
    assert index.publish_scored(
        current,
        FreezeOverlay(
            current.strategy,
            current.trade_date,
            current.version,
            NOW,
            (current.items[0].quote, extra.quote),
            sequence=1,
        ),
        expected_version=None,
    ).accepted
    new_quote = replace(extra.quote, price=11.0, pct_change=2.0)
    overlay = FreezeOverlay(current.strategy, current.trade_date, current.version, NOW, (new_quote,), sequence=2)
    prior = index.snapshot(current.strategy)
    assert prior.overlay is not None
    assert index.publish_overlay(overlay, expected_version=prior.overlay.version).accepted
    assert index.snapshot(current.strategy).current == current
    assert index.snapshot(current.strategy).overlay.quotes[0].pct_change == 2.0


def _quote() -> FreezeQuote:
    return FreezeQuote(
        "600001",
        10.5,
        1.0,
        120_000_000.0,
        2.1,
        12_000_000_000.0,
        "tencent",
        NOW,
        "quote-fixture",
    )


def test_freeze_seal_projects_official_items_and_rejects_all_same_day_updates() -> None:
    index = UnifiedFreezeIndex()
    local = freeze()
    assert index.publish(local, expected_version=None).accepted
    boundary = datetime.combine(local.trade_date, datetime.min.time(), tzinfo=ZoneInfo("Asia/Shanghai")).replace(
        hour=14,
        minute=50,
    )

    sealed = index.seal_for_freeze(Strategy.TOMORROW, boundary_at=boundary)
    retry = index.seal_for_freeze(Strategy.TOMORROW, boundary_at=boundary)

    assert sealed.accepted and sealed.freeze is not None
    assert retry.freeze == sealed.freeze
    assert index.publish(freeze(sequence=3), expected_version=local.version).reason == "freeze_sealed"
