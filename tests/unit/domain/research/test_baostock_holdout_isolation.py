from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

import pytest

from trader.domain.research.baostock_holdout_isolation import (
    BAOSTOCK_DAILY_IDENTITY,
    BAOSTOCK_SOURCE_ANCHOR,
    LEGACY_HOLDOUT_IDENTITY,
    POINT_IN_TIME_HOLDOUT_IDENTITY,
    BaoStockHoldoutIsolationBlocker,
    BaoStockHoldoutIsolationInput,
    audit_baostock_holdout_isolation,
)

_DAILY_HASH = "1" * 64
_SPLIT_HASH = "2" * 64
_LEGACY_HASH = "3" * 64


def _dates(count: int) -> tuple[date, ...]:
    start = date(2020, 1, 1)
    return tuple(start + timedelta(days=index) for index in range(count))


def _valid_input() -> BaoStockHoldoutIsolationInput:
    ordered_dates = _dates(1_250)
    daily_close_dates = ordered_dates[:-200]
    return BaoStockHoldoutIsolationInput(
        daily_identity=BAOSTOCK_DAILY_IDENTITY,
        daily_manifest_hash=_DAILY_HASH,
        split_manifest_hash=_SPLIT_HASH,
        ordered_complete_dates=ordered_dates,
        training_consumed_dates=daily_close_dates[:630],
        confirmation_consumed_dates=daily_close_dates[630:840],
        daily_proxy_consumed_dates=daily_close_dates[840:],
        point_in_time_reserved_dates=ordered_dates[-200:],
        source_anchor=BAOSTOCK_SOURCE_ANCHOR,
        point_in_time_parity_claimed=False,
        point_in_time_holdout_opened=False,
        legacy_holdout_identity=LEGACY_HOLDOUT_IDENTITY,
        legacy_holdout_hash=_LEGACY_HASH,
        new_holdout_identity=POINT_IN_TIME_HOLDOUT_IDENTITY,
        new_holdout_parent_hashes=(_DAILY_HASH, _SPLIT_HASH),
    )


def test_latest_200_dates_are_isolated_from_every_daily_close_consumer() -> None:
    result = audit_baostock_holdout_isolation(_valid_input())

    assert result.status == "isolated"
    assert result.blockers == ()
    assert result.reserved_date_count == 200
    assert result.production_authority is False
    assert result.terminal_holdout_opened is False
    assert result.point_in_time_parity is False


@pytest.mark.parametrize(
    "field",
    ("training_consumed_dates", "confirmation_consumed_dates", "daily_proxy_consumed_dates"),
)
def test_reserved_date_consumed_by_any_daily_close_stage_is_blocked(field: str) -> None:
    value = _valid_input()
    result = audit_baostock_holdout_isolation(
        replace(value, **{field: (*getattr(value, field), value.point_in_time_reserved_dates[0])})
    )

    assert result.status == "blocked"
    assert BaoStockHoldoutIsolationBlocker.POINT_IN_TIME_RESERVE_CONSUMED in result.blockers


def test_reserve_must_be_exactly_the_latest_200_complete_dates() -> None:
    value = _valid_input()

    below_minimum = audit_baostock_holdout_isolation(
        replace(value, point_in_time_reserved_dates=value.point_in_time_reserved_dates[1:])
    )
    not_latest = audit_baostock_holdout_isolation(
        replace(
            value,
            point_in_time_reserved_dates=(
                value.ordered_complete_dates[-201],
                *value.point_in_time_reserved_dates[:-1],
            ),
        )
    )

    assert BaoStockHoldoutIsolationBlocker.POINT_IN_TIME_RESERVE_BELOW_200 in below_minimum.blockers
    assert BaoStockHoldoutIsolationBlocker.POINT_IN_TIME_RESERVE_NOT_LATEST in below_minimum.blockers
    assert not_latest.blockers == (
        BaoStockHoldoutIsolationBlocker.POINT_IN_TIME_RESERVE_NOT_LATEST,
        BaoStockHoldoutIsolationBlocker.POINT_IN_TIME_RESERVE_CONSUMED,
    )


def test_daily_close_source_cannot_claim_1450_point_in_time_parity() -> None:
    value = _valid_input()
    result = audit_baostock_holdout_isolation(
        replace(value, source_anchor="14:50_point_in_time", point_in_time_parity_claimed=True)
    )

    assert result.blockers == (
        BaoStockHoldoutIsolationBlocker.DAILY_SOURCE_ANCHOR_INVALID,
        BaoStockHoldoutIsolationBlocker.DAILY_SOURCE_CLAIMS_POINT_IN_TIME,
    )


def test_audit_blocks_an_already_opened_point_in_time_holdout() -> None:
    result = audit_baostock_holdout_isolation(replace(_valid_input(), point_in_time_holdout_opened=True))

    assert result.blockers == (BaoStockHoldoutIsolationBlocker.POINT_IN_TIME_HOLDOUT_ALREADY_OPENED,)
    assert result.terminal_holdout_opened is False


def test_new_holdout_cannot_reuse_legacy_identity_or_parent_hash() -> None:
    value = _valid_input()
    result = audit_baostock_holdout_isolation(
        replace(
            value,
            new_holdout_identity=LEGACY_HOLDOUT_IDENTITY,
            new_holdout_parent_hashes=(_DAILY_HASH, _SPLIT_HASH, _LEGACY_HASH),
        )
    )

    assert result.blockers == (
        BaoStockHoldoutIsolationBlocker.NEW_HOLDOUT_IDENTITY_MISMATCH,
        BaoStockHoldoutIsolationBlocker.LEGACY_HOLDOUT_REUSED_AS_PARENT,
    )


def test_identity_and_required_new_parent_hashes_are_fixed() -> None:
    value = _valid_input()
    result = audit_baostock_holdout_isolation(
        replace(
            value,
            daily_identity="score_baostock_daily_core_v1",
            legacy_holdout_identity="tomorrow_v3_point_in_time_holdout_v1",
            new_holdout_parent_hashes=(_DAILY_HASH,),
        )
    )

    assert result.blockers == (
        BaoStockHoldoutIsolationBlocker.DAILY_IDENTITY_MISMATCH,
        BaoStockHoldoutIsolationBlocker.LEGACY_HOLDOUT_IDENTITY_MISMATCH,
        BaoStockHoldoutIsolationBlocker.REQUIRED_PARENT_HASH_MISSING,
    )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("daily_manifest_hash", "not-a-hash", "SHA-256"),
        ("ordered_complete_dates", (_dates(2)[1], _dates(2)[0]), "strictly increasing"),
        ("ordered_complete_dates", (*_dates(2), _dates(2)[-1]), "strictly increasing"),
        ("training_consumed_dates", (_dates(2)[1], _dates(2)[0]), "strictly increasing"),
        ("split_manifest_hash", _DAILY_HASH, "must be distinct"),
        ("new_holdout_parent_hashes", (_DAILY_HASH, _DAILY_HASH), "must be unique"),
    ),
)
def test_structurally_invalid_metadata_is_rejected(field: str, value: object, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        replace(_valid_input(), **{field: value})


def test_consumed_and_reserved_dates_must_belong_to_complete_calendar() -> None:
    value = _valid_input()
    outside = value.ordered_complete_dates[-1] + timedelta(days=1)

    with pytest.raises(ValueError, match="complete calendar"):
        replace(value, training_consumed_dates=(*value.training_consumed_dates, outside))
    with pytest.raises(ValueError, match="complete calendar"):
        replace(value, point_in_time_reserved_dates=(*value.point_in_time_reserved_dates[:-1], outside))


def test_same_metadata_produces_a_deterministic_audit_hash() -> None:
    first = audit_baostock_holdout_isolation(_valid_input())
    second = audit_baostock_holdout_isolation(_valid_input())

    assert first.input_content_hash == second.input_content_hash
    assert first.content_hash == second.content_hash
