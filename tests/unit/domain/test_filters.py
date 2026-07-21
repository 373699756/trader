from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

import pytest

from trader.domain.filters import HardFilterPolicy, board_for_code, hard_filter
from trader.domain.models import Board


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("600001", Board.MAIN),
        ("000001", Board.MAIN),
        ("300001", Board.CHINEXT),
        ("688001", Board.STAR),
        ("830001", Board.UNSUPPORTED),
        ("invalid", Board.UNSUPPORTED),
    ],
)
def test_board_for_code(code, expected) -> None:
    assert board_for_code(code) is expected


def test_explicit_board_metadata_cannot_make_an_illegal_code_supported(feature_factory, observed_at) -> None:
    snapshot = feature_factory(code="999999")
    snapshot = replace(snapshot, quote=replace(snapshot.quote, board=Board.MAIN))

    result = hard_filter(snapshot, observed_at, max_age_seconds=20)

    assert result.allowed is False
    assert result.reasons[0].filter_code == "unsupported_code"


@pytest.mark.parametrize(
    ("code", "pct_change", "allowed", "reason"),
    [
        ("600001", 8.00, True, ""),
        ("600001", 8.01, False, "main_board_too_hot"),
        ("300001", 16.00, True, ""),
        ("300001", 16.01, False, "chinext_board_too_hot"),
        ("688001", 16.00, True, ""),
        ("688001", 16.01, False, "star_board_too_hot"),
    ],
)
def test_hot_price_boundaries(feature_factory, observed_at, code, pct_change, allowed, reason) -> None:
    result = hard_filter(feature_factory(code=code, pct_change=pct_change), observed_at, max_age_seconds=20)

    assert result.allowed is allowed
    if reason:
        assert reason in {item.code for item in result.reasons}


def test_filter_reports_source_threshold_and_actual(feature_factory, observed_at) -> None:
    snapshot = feature_factory(values={"amount_median_20d": 49_999_999.0})

    result = hard_filter(snapshot, observed_at, max_age_seconds=20)

    reason = next(item for item in result.reasons if item.code == "insufficient_liquidity")
    assert reason.threshold == ">= 50000000"
    assert reason.actual == 49_999_999.0
    assert reason.source == "fixture"


@pytest.mark.parametrize(
    ("value", "allowed", "reason_code", "actual"),
    [
        (None, False, "missing_liquidity_history", None),
        (float("nan"), False, "invalid_liquidity_history", "nan"),
        (float("inf"), False, "invalid_liquidity_history", "inf"),
        (49_999_999.0, False, "insufficient_liquidity", 49_999_999.0),
        (50_000_000.0, True, "", 50_000_000.0),
    ],
)
def test_liquidity_history_is_required_with_exact_boundary(
    feature_factory,
    observed_at,
    value,
    allowed,
    reason_code,
    actual,
) -> None:
    result = hard_filter(
        feature_factory(values={"amount_median_20d": value}),
        observed_at,
        max_age_seconds=20,
    )

    assert result.allowed is allowed
    if reason_code:
        reason = next(item for item in result.reasons if item.filter_code == reason_code)
        assert reason.threshold == ">= 50000000"
        assert reason.actual == actual
        assert reason.source == "fixture"
        assert reason.observed_at == observed_at


def test_quote_age_boundary_and_future_quote_are_auditable(feature_factory, observed_at) -> None:
    exact = feature_factory()
    exact = replace(exact, quote=replace(exact.quote, source_time=observed_at - timedelta(seconds=20)))
    stale = replace(exact, quote=replace(exact.quote, source_time=observed_at - timedelta(seconds=20.001)))
    future = replace(exact, quote=replace(exact.quote, source_time=observed_at + timedelta(milliseconds=1)))

    assert hard_filter(exact, observed_at, max_age_seconds=20).allowed is True
    assert "stale_quote" in {item.filter_code for item in hard_filter(stale, observed_at, max_age_seconds=20).reasons}
    assert "future_quote" in {item.filter_code for item in hard_filter(future, observed_at, max_age_seconds=20).reasons}


@pytest.mark.parametrize(
    ("field", "value", "expected_code"),
    [
        ("negative_announcement_level", 1.0, "negative_announcement"),
        ("reduction_or_unlock", 1.0, "reduction_or_unlock"),
        ("pledge_risk", 1.0, "pledge_risk"),
        ("financial_deterioration", 1.0, "financial_deterioration"),
    ],
)
def test_structured_negative_risks_are_hard_filters(
    feature_factory, observed_at, field: str, value: float, expected_code: str
) -> None:
    snapshot = feature_factory(values={field: value})

    result = hard_filter(snapshot, observed_at, max_age_seconds=20)

    assert result.allowed is False
    assert expected_code in {item.filter_code for item in result.reasons}


def test_missing_structured_risk_is_audited_without_blocking_local_fallback(feature_factory, observed_at) -> None:
    snapshot = feature_factory(values={"pledge_risk": None})

    result = hard_filter(snapshot, observed_at, max_age_seconds=20)

    assert result.allowed is True
    assert "structured_risk_unavailable" in {item.filter_code for item in result.optional_flags}


def test_configured_blacklist_is_a_hard_filter(feature_factory, observed_at) -> None:
    snapshot = feature_factory(code="600001")

    result = hard_filter(
        snapshot,
        observed_at,
        max_age_seconds=20,
        policy=HardFilterPolicy(blacklist_codes=frozenset({"600001"})),
    )

    assert result.allowed is False
    assert "blacklisted" in {item.filter_code for item in result.reasons}


def test_non_finite_and_structurally_invalid_quotes_are_rejected(feature_factory, observed_at) -> None:
    non_finite = feature_factory(pct_change=float("nan"))
    invalid_ohlc = feature_factory()
    invalid_ohlc = replace(invalid_ohlc, quote=replace(invalid_ohlc.quote, high=11.0, price=12.0))
    invalid_deviation = feature_factory()
    invalid_deviation = replace(
        invalid_deviation,
        quote=replace(invalid_deviation.quote, cross_source_deviation_pct=float("inf")),
    )

    assert "invalid_pct_change" in {
        item.filter_code for item in hard_filter(non_finite, observed_at, max_age_seconds=20).reasons
    }
    assert "invalid_quote_structure" in {
        item.filter_code for item in hard_filter(invalid_ohlc, observed_at, max_age_seconds=20).reasons
    }
    assert "invalid_cross_source_deviation" in {
        item.filter_code for item in hard_filter(invalid_deviation, observed_at, max_age_seconds=20).reasons
    }


@pytest.mark.parametrize(
    ("deviation", "verified", "allowed"),
    [(0.5, False, True), (0.5001, False, True), (0.5001, True, True)],
)
def test_cross_source_deviation_boundary(feature_factory, observed_at, deviation, verified, allowed) -> None:
    snapshot = feature_factory()
    snapshot = replace(
        snapshot,
        quote=replace(
            snapshot.quote,
            cross_source_deviation_pct=deviation,
            cross_source_verified=verified,
        ),
    )

    assert hard_filter(snapshot, observed_at, max_age_seconds=20).allowed is allowed


@pytest.mark.parametrize(("sessions", "allowed"), [(0, False), (1, False), (5, False), (6, True)])
def test_listing_session_boundary_is_enforced(feature_factory, observed_at, sessions, allowed) -> None:
    snapshot = feature_factory(
        board=Board.MAIN,
        board_source="tushare",
        board_reliability="verified",
        listing_date=date(2026, 7, 10),
        listing_age_sessions=sessions,
        has_price_limit=sessions >= 6,
        exchange_limit_pct=10.0 if sessions >= 6 else None,
        rule_version="cn-board-rules-v1",
        rule_effective_date=date(2023, 8, 28),
    )

    result = hard_filter(snapshot, observed_at, max_age_seconds=20)

    assert result.allowed is allowed
    if not allowed:
        assert "new_listing_session" in {reason.filter_code for reason in result.reasons}


def test_board_identity_conflict_and_prefix_fallback_are_observe_only(feature_factory, observed_at) -> None:
    conflict = feature_factory(
        board=Board.MAIN,
        board_source="conflict",
        board_reliability="conflict",
        execution_restrictions=("board_classification_conflict",),
    )
    fallback = feature_factory(
        board=Board.MAIN,
        board_source="code_prefix_fallback",
        board_reliability="degraded",
        execution_restrictions=("board_identity_degraded",),
    )
    missing_age = feature_factory(
        board=Board.MAIN,
        board_source="tushare",
        board_reliability="verified",
        listing_date=date(2020, 1, 2),
        listing_age_sessions=None,
        execution_restrictions=("missing_listing_age_sessions",),
    )

    conflict_result = hard_filter(conflict, observed_at, max_age_seconds=20)
    fallback_result = hard_filter(fallback, observed_at, max_age_seconds=20)
    missing_age_result = hard_filter(missing_age, observed_at, max_age_seconds=20)

    assert conflict_result.allowed is True
    assert fallback_result.allowed is True
    assert missing_age_result.allowed is True
    assert "board_classification_conflict" in {item.filter_code for item in conflict_result.optional_flags}
    assert "board_identity_degraded" in {item.filter_code for item in fallback_result.optional_flags}
    assert "missing_listing_age_sessions" in {item.filter_code for item in missing_age_result.optional_flags}


@pytest.mark.parametrize(
    ("field", "reason"),
    [
        ("is_relisted_first_session", "relisted_first_session"),
        ("is_delisting_period_first_session", "delisting_period_first_session"),
    ],
)
def test_special_first_session_states_are_rejected(feature_factory, observed_at, field, reason) -> None:
    snapshot = feature_factory(**{field: True})

    result = hard_filter(snapshot, observed_at, max_age_seconds=20)

    assert result.allowed is False
    assert reason in {item.filter_code for item in result.reasons}
