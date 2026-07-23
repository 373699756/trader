from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from trader.application.cache import canonical_json_bytes
from trader.domain.market.models import CanonicalMarketSnapshot
from trader.infra.market_data import columnar_merge as columnar_merge_module
from trader.infra.market_data.columnar_merge import (
    CompleteRealtimeNormalization,
    columnar_merge_epoch,
    try_merge_complete_realtime,
    try_normalize_complete_realtime_rows,
)
from trader.infra.market_data.merge import (
    merge_market_observations,
    observation_from_quote,
    overlay_canonical_snapshot,
    snapshot_payload_hash,
)
from trader.infra.market_data.merge_quote import merge_code
from trader.infra.market_data.normalize import MarketQuoteInput, build_market_quote
from trader.infra.market_data.observations import SourceObservation

SHANGHAI = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 7, 16, 10, 0, tzinfo=SHANGHAI)


def _observation(
    source: str,
    *,
    price: float = 10.0,
    observed_at: datetime = NOW,
    source_time: datetime = NOW,
    received_at: datetime = NOW,
    data_version: str | None = None,
    status: str = "success",
) -> SourceObservation:
    return SourceObservation(
        source=source,
        subject_key="600001",
        observed_at=observed_at,
        source_time=source_time,
        received_at=received_at,
        effective_at=source_time,
        data_version=data_version if data_version is not None else f"{source}-v1",
        fields={
            "name": "测试股份",
            "price": price,
            "previous_close": 9.8,
            "open_price": 9.9,
            "high": max(price, 10.1),
            "low": 9.7,
            "pct_change": 2.0,
            "amount": 300_000_000.0,
            "industry": "工业",
        },
        missing_reasons={},
        payload_hash=f"{source}-payload",
        status=status,
        error_code=None if status == "success" else "offline",
    )


def test_merge_is_deterministic_and_prefers_eastmoney_at_equal_time() -> None:
    eastmoney = _observation("eastmoney", price=10.0)
    sina = _observation("sina", price=10.04)

    first = merge_market_observations((sina, eastmoney), observed_at=NOW)
    second = merge_market_observations((eastmoney, sina), observed_at=NOW)

    assert isinstance(first, CanonicalMarketSnapshot)
    assert first == second
    assert first.quotes[0].price == 10.0
    assert first.field_sources["600001"]["price"] == "eastmoney"
    assert first.quotes[0].cross_source_deviation_pct == 0.4
    assert snapshot_payload_hash(first) == snapshot_payload_hash(second)
    assert snapshot_payload_hash(first) == "269afe7618c3fa25b252792ffce1703da8eab97ab3a3b45614b47b9a6fae6ed9"
    assert first.merge_epoch == "a48cc036aab1cad6abfc1ea7"


def test_complete_realtime_columnar_projection_matches_scalar_field_merge() -> None:
    complete_fields = {
        "name": "测试股份",
        "price": 10.0,
        "previous_close": 9.8,
        "open_price": 9.9,
        "high": 10.1,
        "low": 9.7,
        "pct_change": 2.0,
        "change_5m": 0.2,
        "speed": 0.3,
        "volume_ratio": 1.4,
        "turnover_rate": 2.0,
        "amount": 300_000_000.0,
        "amplitude": 4.0,
        "market_cap": 20_000_000_000.0,
        "industry": "工业",
        "is_st": False,
        "is_suspended": False,
        "is_one_price_limit": False,
        "is_blacklisted": False,
        "has_major_regulatory_risk": False,
    }
    eastmoney = replace(_observation("eastmoney"), fields=complete_fields)
    sina = replace(
        _observation("sina", price=10.04),
        fields={**complete_fields, "price": 10.04},
    )

    projection = try_merge_complete_realtime((sina, eastmoney))
    expected_quote, expected_sources, expected_conflicts = merge_code(
        "600001",
        (sina, eastmoney),
        targeted=False,
    )

    assert projection is not None
    assert projection.quotes == (expected_quote,)
    assert projection.field_sources == {"600001": expected_sources}
    assert projection.source_versions == {"eastmoney": "eastmoney-v1", "sina": "sina-v1"}
    assert projection.conflicts == tuple(sorted(expected_conflicts))
    canonical_projection = {
        "observed_at": NOW,
        "quotes": projection.quotes,
        "field_sources": projection.field_sources,
        "source_versions": projection.source_versions,
        "conflicts": projection.conflicts,
        "missing_reasons": {},
    }
    expected_epoch = hashlib.sha256(canonical_json_bytes(canonical_projection)).hexdigest()[:24]
    assert columnar_merge_epoch(NOW, projection, {}) == expected_epoch
    assert try_merge_complete_realtime((eastmoney, replace(sina, fields={"price": 10.04}))) is None
    assert try_merge_complete_realtime((replace(eastmoney, fields={**complete_fields, "price": "10.0"}), sina)) is None
    assert try_merge_complete_realtime((eastmoney, replace(sina, status="failed", error_code="offline"))) is None


def test_columnar_merge_failure_uses_scalar_projection_and_marks_degraded(monkeypatch) -> None:
    complete_fields = {
        **_observation("eastmoney").fields,
        "change_5m": 0.2,
        "speed": 0.3,
        "volume_ratio": 1.4,
        "turnover_rate": 2.0,
        "amplitude": 4.0,
        "market_cap": 20_000_000_000.0,
        "is_st": False,
        "is_suspended": False,
        "is_one_price_limit": False,
        "is_blacklisted": False,
        "has_major_regulatory_risk": False,
    }
    eastmoney = replace(_observation("eastmoney"), fields=complete_fields)
    sina = replace(_observation("sina", price=10.04), fields={**complete_fields, "price": 10.04})

    def fail_winner_projection(*_args, **_kwargs):
        raise columnar_merge_module.pl.exceptions.ComputeError("injected columnar merge failure")

    monkeypatch.setattr(columnar_merge_module, "_winner_indexes", fail_winner_projection)

    snapshot = merge_market_observations((sina, eastmoney), observed_at=NOW)

    assert snapshot.quotes[0].price == 10.0
    assert "columnar_merge_failed:scalar_fallback" in snapshot.degraded_reasons


def test_columnar_normalization_matches_scalar_quote_observation() -> None:
    row = {
        "code": "600001",
        "name": "测试股份",
        "price": 10.0,
        "previous_close": 9.8,
        "open_price": 9.9,
        "high": 10.1,
        "low": 9.7,
        "pct_change": 2.0,
        "change_5m": 0.2,
        "speed": 0.3,
        "volume_ratio": 1.4,
        "turnover_rate": 2.0,
        "amount": 300_000_000.0,
        "amplitude": 4.0,
        "market_cap": 20_000_000_000.0,
        "industry": "工业",
    }
    scalar_quote = build_market_quote(
        MarketQuoteInput(
            code="600001",
            name="测试股份",
            price=10.04,
            previous_close=9.8,
            open_price=9.9,
            high=10.1,
            low=9.7,
            pct_change=2.0,
            change_5m=0.2,
            speed=0.3,
            volume_ratio=1.4,
            turnover_rate=2.0,
            amount=300_000_000.0,
            amplitude=4.0,
            market_cap=20_000_000_000.0,
            industry="工业",
            source="sina",
            source_time=NOW,
            received_time=NOW,
            data_version="sina-v1",
        )
    )

    columnar = try_normalize_complete_realtime_rows(
        (row,),
        CompleteRealtimeNormalization(
            source="sina",
            observed_at=NOW,
            source_time=NOW,
            received_at=NOW,
            data_version="sina-v1",
            price_multiplier=1.004,
        ),
    )

    assert columnar == (observation_from_quote(scalar_quote, source="sina", observed_at=NOW),)
    assert (
        try_normalize_complete_realtime_rows(
            ({**row, "price": "10.0"},),
            CompleteRealtimeNormalization(
                source="sina",
                observed_at=NOW,
                source_time=NOW,
                received_at=NOW,
                data_version="sina-v1",
                price_multiplier=1.004,
            ),
        )
        is None
    )


def test_same_source_equal_version_uses_payload_hash_as_deterministic_tie_breaker() -> None:
    lower_hash = replace(_observation("eastmoney", price=10.0), payload_hash="a-payload")
    higher_hash = replace(_observation("eastmoney", price=10.1), payload_hash="z-payload")

    first = merge_market_observations((lower_hash, higher_hash), observed_at=NOW)
    second = merge_market_observations((higher_hash, lower_hash), observed_at=NOW)

    assert first == second
    assert first.quotes[0].price == 10.1
    assert snapshot_payload_hash(first) == snapshot_payload_hash(second)


def test_equal_time_cross_source_overlay_is_direction_independent() -> None:
    eastmoney = merge_market_observations((_observation("eastmoney", price=10.0),), observed_at=NOW)
    sina = merge_market_observations((_observation("sina", price=10.1),), observed_at=NOW)

    eastmoney_base = overlay_canonical_snapshot(eastmoney, sina)
    sina_base = overlay_canonical_snapshot(sina, eastmoney)

    assert eastmoney_base == sina_base
    assert eastmoney_base.quotes[0].source == "eastmoney"
    assert eastmoney_base.quotes[0].price == 10.0


def test_merge_rejects_future_empty_version_and_late_observations() -> None:
    valid = _observation("eastmoney")
    future = _observation("sina", source_time=NOW + timedelta(microseconds=1))
    future_observed_at = _observation("akshare", observed_at=NOW + timedelta(microseconds=1))
    empty_version = _observation("tencent", data_version="")
    late = _observation("sina", status="late")

    snapshot = merge_market_observations(
        (future, future_observed_at, empty_version, late, valid),
        observed_at=NOW,
    )

    assert snapshot.quotes[0].source == "eastmoney"
    assert set(snapshot.degraded_reasons) == {
        "akshare:future_observation",
        "sina:future_observation",
        "sina:late",
        "tencent:empty_data_version",
    }


def test_missing_price_source_fallback_uses_source_priority_before_cross_vendor_version() -> None:
    eastmoney = replace(
        _observation("eastmoney", data_version="eastmoney-a"),
        fields={"name": "测试股份"},
        payload_hash="eastmoney-name-only",
    )
    sina = replace(
        _observation("sina", data_version="sina-z"),
        fields={"name": "测试股份"},
        payload_hash="sina-name-only",
    )

    snapshot = merge_market_observations((sina, eastmoney), observed_at=NOW)

    assert snapshot.quotes[0].source == "eastmoney"


def test_source_version_uses_observation_time_before_lexical_version_order() -> None:
    older = _observation(
        "eastmoney",
        price=9.0,
        source_time=NOW - timedelta(seconds=1),
        received_at=NOW - timedelta(seconds=1),
        data_version="z-older",
    )
    newer = _observation("eastmoney", price=10.0, data_version="a-newer")

    snapshot = merge_market_observations((older, newer), observed_at=NOW)

    assert snapshot.quotes[0].price == 10.0
    assert snapshot.source_versions["eastmoney"] == "a-newer"


def test_older_overlay_cannot_replace_newer_quote_or_source_version() -> None:
    older_at = NOW - timedelta(seconds=1)
    older = merge_market_observations(
        (
            _observation(
                "eastmoney",
                price=9.0,
                observed_at=older_at,
                source_time=older_at,
                received_at=older_at,
                data_version="z-older",
            ),
        ),
        observed_at=older_at,
    )
    older = replace(
        older,
        conflicts=("price_divergence:600001",),
        missing_reasons={"600001.price.sina": "stale_overlay"},
        degraded_reasons=("sina:late",),
    )
    newer = merge_market_observations(
        (_observation("eastmoney", price=10.0, data_version="a-newer"),),
        observed_at=NOW,
    )

    combined = overlay_canonical_snapshot(newer, older)

    assert combined.quotes[0].price == 10.0
    assert combined.source_versions["eastmoney"] == "a-newer"
    assert combined.conflicts == ()
    assert combined.missing_reasons == {}
    assert combined.degraded_reasons == ()


def test_later_overlay_that_does_not_replace_quote_cannot_regress_source_version() -> None:
    current = merge_market_observations(
        (_observation("eastmoney", price=10.0, data_version="a-current"),),
        observed_at=NOW,
    )
    later_at = NOW + timedelta(seconds=1)
    stale = merge_market_observations(
        (
            _observation(
                "eastmoney",
                price=9.0,
                observed_at=later_at,
                source_time=NOW - timedelta(seconds=1),
                received_at=NOW - timedelta(seconds=1),
                data_version="z-stale",
            ),
        ),
        observed_at=later_at,
    )

    combined = overlay_canonical_snapshot(current, stale)

    assert combined.quotes[0].price == 10.0
    assert combined.source_versions["eastmoney"] == "a-current"


def test_older_partial_overlay_cannot_regress_same_source_version_when_adding_a_code() -> None:
    current = merge_market_observations(
        (_observation("eastmoney", price=10.0, data_version="a-current"),),
        observed_at=NOW,
    )
    older_at = NOW - timedelta(seconds=1)
    older_code = replace(
        _observation(
            "eastmoney",
            price=9.0,
            observed_at=older_at,
            source_time=older_at,
            received_at=older_at,
            data_version="z-older",
        ),
        subject_key="600002",
    )
    partial = merge_market_observations((older_code,), observed_at=NOW + timedelta(seconds=1))

    combined = overlay_canonical_snapshot(current, partial)

    assert tuple(quote.code for quote in combined.quotes) == ("600001", "600002")
    assert combined.source_versions["eastmoney"] == "a-current"


def test_invalid_subject_key_does_not_enter_canonical_snapshot_metadata() -> None:
    invalid = replace(_observation("eastmoney"), subject_key="not-a-code")

    snapshot = merge_market_observations((invalid,), observed_at=NOW)

    assert snapshot.quotes == ()
    assert snapshot.source_versions == {}
    assert snapshot.missing_reasons == {}
    assert "invalid_subject_key:not-a-code" in snapshot.degraded_reasons


def test_unverified_price_divergence_above_half_percent_is_observe_only() -> None:
    snapshot = merge_market_observations(
        (_observation("eastmoney", price=10.0), _observation("sina", price=10.051)),
        observed_at=NOW,
    )

    quote = snapshot.quotes[0]
    assert quote.cross_source_deviation_pct == 0.51
    assert quote.cross_source_verified is False
    assert "price_divergence:600001" in snapshot.conflicts
    assert "cross_source_deviation" in quote.execution_restrictions


def test_price_divergence_exactly_half_percent_remains_verified() -> None:
    snapshot = merge_market_observations(
        (_observation("eastmoney", price=10.0), _observation("sina", price=10.05)),
        observed_at=NOW,
    )

    quote = snapshot.quotes[0]
    assert quote.cross_source_deviation_pct == 0.5
    assert quote.cross_source_verified is True
    assert "price_divergence:600001" not in snapshot.conflicts


def test_targeted_quote_must_agree_with_a_full_market_source_to_verify_divergence() -> None:
    snapshot = merge_market_observations(
        (
            _observation("eastmoney", price=10.0),
            _observation("sina", price=10.01),
            _observation("tencent", price=10.2),
        ),
        observed_at=NOW,
        targeted_codes=("600001",),
    )

    quote = snapshot.quotes[0]
    assert quote.price == 10.2
    assert quote.cross_source_verified is False
    assert "price_divergence:600001" in snapshot.conflicts
    assert "cross_source_deviation" in quote.execution_restrictions


def test_targeted_quote_just_over_half_percent_cannot_pass_with_a_larger_denominator() -> None:
    snapshot = merge_market_observations(
        (
            _observation("eastmoney", price=10.0),
            _observation("sina", price=9.99),
            _observation("tencent", price=10.0501),
        ),
        observed_at=NOW,
        targeted_codes=("600001",),
    )

    quote = snapshot.quotes[0]
    assert quote.cross_source_deviation_pct > 0.5
    assert quote.cross_source_verified is False
    assert "price_divergence:600001" in snapshot.conflicts
    assert "cross_source_deviation" in quote.execution_restrictions


def test_all_sources_failed_preserves_last_valid_snapshot() -> None:
    previous = merge_market_observations((_observation("eastmoney"),), observed_at=NOW)
    failed = _observation("eastmoney", status="failed")

    recovered = merge_market_observations(
        (failed,),
        observed_at=NOW + timedelta(seconds=10),
        previous=previous,
    )

    assert recovered.quotes == previous.quotes
    assert recovered.merge_epoch == previous.merge_epoch
    assert recovered.observed_at == previous.observed_at
    assert "all_sources_failed:last_valid_snapshot" in recovered.degraded_reasons


def test_slow_source_cannot_overwrite_realtime_price_and_can_supply_board_identity() -> None:
    realtime = _observation("eastmoney", price=10.0)
    tushare = SourceObservation(
        source="tushare",
        subject_key="600001",
        observed_at=NOW,
        source_time=NOW,
        received_at=NOW,
        effective_at=NOW - timedelta(days=1),
        data_version="tushare-master-v1",
        fields={
            "price": 99.0,
            "board": "main",
            "exchange": "SSE",
            "listing_date": "2020-01-02",
            "listing_age_sessions": 1000.0,
            "has_price_limit": True,
            "exchange_limit_pct": 10.0,
            "rule_version": "cn-board-rules-v1",
            "rule_effective_date": "2023-08-28",
        },
        missing_reasons={},
        payload_hash="master-payload",
        status="success",
        error_code=None,
    )

    snapshot = merge_market_observations((tushare, realtime), observed_at=NOW)

    quote = snapshot.quotes[0]
    assert quote.price == 10.0
    assert quote.board.value == "main"
    assert quote.board_source == "tushare"
    assert quote.board_reliability == "verified"
    assert quote.strategy_hot_cap_pct == 8.0
    assert quote.execution_restrictions == ()
    assert snapshot.field_sources["600001"]["board_reliability"] == "tushare"
    assert snapshot.field_sources["600001"]["listing_age_sessions"] == "trading_calendar"
    assert snapshot.field_sources["600001"]["exchange_limit_pct"] == "local_rule"
    assert snapshot.field_sources["600001"]["strategy_hot_cap_pct"] == "local_rule"

    fields_without_age = dict(tushare.fields)
    fields_without_age.pop("listing_age_sessions")
    missing_age = merge_market_observations(
        (replace(tushare, fields=fields_without_age, payload_hash="master-without-age"), realtime),
        observed_at=NOW,
    )
    assert "missing_listing_age_sessions" in missing_age.quotes[0].execution_restrictions
