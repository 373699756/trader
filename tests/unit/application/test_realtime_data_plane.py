from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from tests.unit.v2_epoch_helpers import (
    candidate_field_values,
    coverage,
    daily_field_values,
    market_field_values,
    research_field_values,
)
from trader.application.realtime_data_plane import DataPlaneChannel, RealtimeDataPlane
from trader.domain.market.epochs import (
    CandidateQuoteEpoch,
    DailyFeaturePack,
    DailyFeatureRow,
    MarketEpoch,
    ResearchEpoch,
)
from trader.domain.market.models import Board, LiveQuote, MarketQuote
from trader.domain.market.research import ResearchObservation

SHANGHAI = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 7, 28, 10, 0, tzinfo=SHANGHAI)


def _pack(sequence: int) -> DailyFeaturePack:
    observed_at = NOW + timedelta(seconds=sequence)
    received_at = observed_at + timedelta(milliseconds=100)
    values = {"ma20": 9.5 + sequence / 100}
    return DailyFeaturePack(
        trade_date=NOW.date(),
        sequence=sequence,
        observed_at=observed_at,
        received_at=received_at,
        config_version="runtime-v2",
        calendar_version="calendar-v1",
        rows=(
            DailyFeatureRow(
                code="600001",
                values=values,
                history_sessions=61,
                data_as_of=date(2026, 7, 27),
                field_values=daily_field_values(
                    values,
                    source_time=NOW - timedelta(days=1),
                    received_time=observed_at,
                    data_version=f"history-{sequence}",
                ),
            ),
        ),
        source_versions={"history": f"history-{sequence}"},
        coverage=coverage(("600001",)),
    )


def _quote(sequence: int) -> MarketQuote:
    observed_at = NOW + timedelta(seconds=sequence)
    return MarketQuote(
        code="600001",
        name="stock",
        price=10.0 + sequence / 100,
        previous_close=9.8,
        open_price=9.9,
        high=10.2,
        low=9.7,
        pct_change=2.0,
        change_5m=0.1,
        speed=0.2,
        volume_ratio=1.2,
        turnover_rate=2.5,
        amount=100_000_000.0,
        amplitude=4.0,
        market_cap=10_000_000_000.0,
        industry="industry",
        source="eastmoney",
        source_time=observed_at,
        received_time=observed_at + timedelta(milliseconds=100),
        data_version=f"quote-{sequence}",
        board=Board.MAIN,
    )


def _market(pack: DailyFeaturePack, sequence: int) -> MarketEpoch:
    quote = _quote(sequence)
    return MarketEpoch(
        trade_date=pack.trade_date,
        sequence=sequence,
        observed_at=NOW + timedelta(seconds=sequence),
        received_at=NOW + timedelta(seconds=sequence, milliseconds=100),
        config_version="runtime-v2",
        daily_feature_pack_version=pack.version,
        quotes=(quote,),
        source_versions={"eastmoney": f"market-{sequence}"},
        field_values={quote.code: market_field_values(quote)},
    )


def _candidate(market: MarketEpoch, sequence: int) -> CandidateQuoteEpoch:
    observed_at = NOW + timedelta(seconds=sequence)
    quote = LiveQuote(
        code="600001",
        price=10.1,
        pct_change=3.0,
        source="tencent",
        source_time=observed_at,
        received_time=observed_at + timedelta(milliseconds=100),
        data_version=f"candidate-{sequence}",
        cross_source_deviation_pct=0.2,
        cross_source_verified=True,
    )
    return CandidateQuoteEpoch(
        trade_date=market.trade_date,
        sequence=sequence,
        observed_at=observed_at,
        received_at=observed_at + timedelta(milliseconds=100),
        config_version="runtime-v2",
        market_epoch_version=market.version,
        quotes=(quote,),
        source_versions={"tencent": f"candidate-{sequence}"},
        field_values={quote.code: candidate_field_values(quote)},
    )


def _research(sequence: int, *, config_version: str = "runtime-v2") -> ResearchEpoch:
    observed_at = NOW + timedelta(seconds=sequence)
    received_at = observed_at + timedelta(milliseconds=100)
    return ResearchEpoch(
        trade_date=NOW.date(),
        sequence=sequence,
        observed_at=observed_at,
        received_at=received_at,
        config_version=config_version,
        observations={"600001": ResearchObservation(announcements_available=True)},
        source_versions={"issuer": f"research-{sequence}"},
        field_values={
            "600001": research_field_values(
                source_time=observed_at,
                received_time=received_at,
                data_version=f"research-{sequence}",
            )
        },
    )


def test_data_plane_publishes_only_coherent_monotonic_epochs() -> None:
    plane = RealtimeDataPlane(retained_epochs_per_channel=3)
    first_pack = _pack(1)
    first_market = _market(first_pack, 1)

    missing_parent = plane.publish_market(first_market)
    assert missing_parent.accepted is False
    assert missing_parent.reason == "daily_feature_pack_not_current"

    assert plane.publish_daily_features(first_pack).accepted is True
    assert plane.publish_market(first_market).accepted is True
    assert plane.publish_candidate_quotes(_candidate(first_market, 1)).accepted is True

    stale_pack = _pack(0)
    stale = plane.publish_daily_features(stale_pack)
    assert stale.accepted is False
    assert stale.reason == "stale_epoch"
    assert plane.snapshot().daily_features == first_pack

    conflicting_pack = DailyFeaturePack(
        trade_date=first_pack.trade_date,
        sequence=first_pack.sequence,
        observed_at=first_pack.observed_at,
        received_at=first_pack.received_at,
        config_version="runtime-v2",
        calendar_version="calendar-v1",
        rows=(
            DailyFeatureRow(
                code="600001",
                values={"ma20": 99.0},
                history_sessions=61,
                data_as_of=date(2026, 7, 27),
                field_values=daily_field_values(
                    {"ma20": 99.0},
                    source_time=NOW - timedelta(days=1),
                    received_time=first_pack.observed_at,
                ),
            ),
        ),
        source_versions=first_pack.source_versions,
        coverage=coverage(("600001",)),
    )
    conflict = plane.publish_daily_features(conflicting_pack)
    assert conflict.accepted is False
    assert conflict.reason == "sequence_conflict"
    assert plane.snapshot().daily_features == first_pack


def test_new_feature_pack_does_not_tear_the_last_coherent_market_view() -> None:
    plane = RealtimeDataPlane(retained_epochs_per_channel=3)
    first_pack = _pack(1)
    first_market = _market(first_pack, 1)
    first_candidate = _candidate(first_market, 1)
    plane.publish_daily_features(first_pack)
    plane.publish_market(first_market)
    plane.publish_candidate_quotes(first_candidate)

    second_pack = _pack(2)
    plane.publish_daily_features(second_pack)
    before_market = plane.snapshot()

    assert before_market.daily_features == first_pack
    assert before_market.market == first_market
    assert before_market.candidate_quotes == first_candidate

    second_market = _market(second_pack, 2)
    plane.publish_market(second_market)
    after_market = plane.snapshot()

    assert after_market.daily_features == second_pack
    assert after_market.market == second_market
    assert after_market.candidate_quotes is None


def test_candidate_epoch_must_reference_the_current_market_epoch() -> None:
    plane = RealtimeDataPlane(retained_epochs_per_channel=3)
    first_pack = _pack(1)
    first_market = _market(first_pack, 1)
    plane.publish_daily_features(first_pack)
    plane.publish_market(first_market)

    second_pack = _pack(2)
    second_market = _market(second_pack, 2)
    plane.publish_daily_features(second_pack)
    plane.publish_market(second_market)

    rejected = plane.publish_candidate_quotes(_candidate(first_market, 2))

    assert rejected.accepted is False
    assert rejected.reason == "market_epoch_not_current"
    assert plane.snapshot().candidate_quotes is None


def test_parent_and_child_epochs_must_use_the_same_config_version() -> None:
    plane = RealtimeDataPlane(retained_epochs_per_channel=3)
    pack = _pack(1)
    plane.publish_daily_features(pack)
    quote = _quote(1)
    mismatched = MarketEpoch(
        trade_date=pack.trade_date,
        sequence=1,
        observed_at=NOW + timedelta(seconds=1),
        received_at=NOW + timedelta(seconds=1, milliseconds=100),
        config_version="runtime-other",
        daily_feature_pack_version=pack.version,
        quotes=(quote,),
        source_versions={"eastmoney": "market-1"},
        field_values={quote.code: market_field_values(quote)},
    )

    result = plane.publish_market(mismatched)

    assert result.accepted is False
    assert result.reason == "config_version_mismatch"
    assert plane.snapshot().market is None


def test_snapshot_hides_research_until_trade_date_and_config_match_daily_features() -> None:
    plane = RealtimeDataPlane(retained_epochs_per_channel=3)
    plane.publish_daily_features(_pack(1))
    plane.publish_research(_research(1, config_version="runtime-other"))

    assert plane.snapshot().research is None

    matching = _research(2)
    plane.publish_research(matching)

    assert plane.snapshot().research == matching


def test_failure_status_preserves_last_valid_epoch_and_success_clears_channel_failure() -> None:
    plane = RealtimeDataPlane(retained_epochs_per_channel=3)
    pack = _pack(1)
    plane.publish_daily_features(pack)

    plane.record_failure(
        DataPlaneChannel.DAILY_FEATURES,
        reason="source_timeout",
        observed_at=NOW + timedelta(seconds=2),
    )
    degraded = plane.snapshot()

    assert degraded.daily_features == pack
    assert degraded.failures[DataPlaneChannel.DAILY_FEATURES].reason == "source_timeout"

    next_pack = _pack(2)
    plane.publish_daily_features(next_pack)

    assert DataPlaneChannel.DAILY_FEATURES not in plane.snapshot().failures


def test_failure_status_rejects_unstructured_error_text() -> None:
    plane = RealtimeDataPlane(retained_epochs_per_channel=3)

    with pytest.raises(ValueError, match="structured code"):
        plane.record_failure(
            DataPlaneChannel.MARKET,
            reason="timeout: token=secret",
            observed_at=NOW,
        )


def test_epoch_retention_is_bounded_in_memory_without_disk_archive() -> None:
    plane = RealtimeDataPlane(retained_epochs_per_channel=2)
    for sequence in range(1, 5):
        plane.publish_daily_features(_pack(sequence))

    assert plane.retained_versions(DataPlaneChannel.DAILY_FEATURES) == (
        _pack(3).version,
        _pack(4).version,
    )


def test_concurrent_publication_keeps_the_highest_sequence() -> None:
    plane = RealtimeDataPlane(retained_epochs_per_channel=3)
    packs = tuple(_pack(sequence) for sequence in range(1, 33))

    with ThreadPoolExecutor(max_workers=8) as executor:
        tuple(executor.map(plane.publish_daily_features, reversed(packs)))

    assert plane.snapshot().daily_features == packs[-1]
