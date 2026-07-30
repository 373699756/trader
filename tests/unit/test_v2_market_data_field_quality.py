"""Field-level merge selector tests for deterministic P2 behavior."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from trader.domain.market.quality import FieldQualityState
from trader.infra.market_data.field_quality import (
    BOARD_FIELDS,
    REALTIME_FIELDS,
    normalize_source,
    select_fields,
)
from trader.infra.market_data.observations import JsonScalar, SourceObservation

SHANGHAI = ZoneInfo("Asia/Shanghai")
OBSERVED_AT = datetime(2026, 7, 16, 10, 0, tzinfo=SHANGHAI)


def _observation(
    source: str,
    *,
    payload_hash: str,
    source_time: datetime = OBSERVED_AT,
    received_at: datetime = OBSERVED_AT,
    value: JsonScalar = 10.0,
) -> SourceObservation:
    return SourceObservation(
        source=source,
        subject_key="600001",
        observed_at=source_time,
        source_time=source_time,
        received_at=received_at,
        effective_at=source_time,
        data_version="v1",
        fields={"price": value, "name": "测试股份"},
        missing_reasons={},
        payload_hash=payload_hash,
        status="success",
        error_code=None,
    )


def test_field_selection_is_order_independent() -> None:
    baseline = _observation("sina", payload_hash="z")
    improved = _observation("eastmoney", payload_hash="a", value=10.1)

    first = select_fields((baseline, improved), targeted=False)
    second = select_fields((improved, baseline), targeted=False)

    assert first.values == second.values
    assert first.sources == second.sources
    assert first.quality == second.quality
    assert first.values["price"] == 10.1
    assert first.sources["price"] == "eastmoney"


def test_field_selection_targets_tencent_when_targeted_code() -> None:
    eastmoney = _observation("eastmoney", payload_hash="z", value=10.0)
    tencent = _observation("tencent", payload_hash="a", value=10.5)

    non_targeted = select_fields((eastmoney, tencent), targeted=False)
    targeted = select_fields((eastmoney, tencent), targeted=True)

    assert non_targeted.values["price"] == 10.0
    assert non_targeted.sources["price"] == "eastmoney"
    assert targeted.values["price"] == 10.5
    assert targeted.sources["price"] == "tencent"


def test_source_aliases_are_normalized_before_realtime_priority() -> None:
    eastmoney = _observation("eastmoney", payload_hash="z", value=10.0)
    tencent_long = _observation("tencent_long", payload_hash="a", value=10.5)

    non_targeted = select_fields((eastmoney, tencent_long), targeted=False)
    targeted = select_fields((eastmoney, tencent_long), targeted=True)

    assert normalize_source("tencent_long") == "tencent"
    assert non_targeted.values["price"] == 10.0
    assert non_targeted.sources["price"] == "eastmoney"
    assert targeted.values["price"] == 10.5
    assert targeted.sources["price"] == "tencent"


def test_unadmitted_mootdx_shadow_source_cannot_write_realtime_fields() -> None:
    shadow = _observation("mootdx_shadow", payload_hash="a", value=99.0)
    eastmoney = _observation("eastmoney", payload_hash="b", value=10.0)

    selected = select_fields((shadow, eastmoney), targeted=True)

    assert normalize_source("mootdx_shadow") == "mootdx"
    assert selected.values["price"] == 10.0
    assert selected.sources["price"] == "eastmoney"


def test_time_rollback_is_rejected_per_source() -> None:
    latest = _observation(
        "eastmoney",
        payload_hash="z",
        source_time=OBSERVED_AT,
        received_at=OBSERVED_AT,
        value=10.1,
    )
    stale = _observation(
        "eastmoney",
        payload_hash="a",
        source_time=OBSERVED_AT,
        received_at=OBSERVED_AT,
        value=9.9,
    )
    older = _observation(
        "eastmoney",
        payload_hash="older",
        source_time=OBSERVED_AT - timedelta(microseconds=500_000),
        received_at=OBSERVED_AT - timedelta(microseconds=500_000),
        value=9.8,
    )

    first = select_fields((older, stale, latest), targeted=False)
    second = select_fields((latest, stale, older), targeted=False)

    assert first.values["price"] == 10.1
    assert second.values["price"] == 10.1


def test_field_selection_blocks_disallowed_source_for_realtime_field() -> None:
    blocked = _observation("invalid-source", payload_hash="a", value=8.8)
    winner = _observation("eastmoney", payload_hash="b", value=10.1)

    selected = select_fields((blocked, winner), targeted=False)

    assert selected.values["price"] == 10.1
    assert selected.field_values["price"].source == "eastmoney"


def test_board_and_realtime_field_sets_expose_contract_constants() -> None:
    assert {"board", "exchange"} <= BOARD_FIELDS
    assert "price" in REALTIME_FIELDS


def test_conflict_state_is_exposed_when_value_disagrees() -> None:
    first = _observation("eastmoney", payload_hash="same", value=10.1, source_time=OBSERVED_AT)
    second = _observation(
        "eastmoney",
        payload_hash="same",
        value=10.2,
        source_time=OBSERVED_AT,
        received_at=OBSERVED_AT,
    )

    selected = select_fields((first, second), targeted=False)

    assert selected.conflicts == ("price:conflict",)
    assert selected.values["price"] in (10.1, 10.2)
    assert selected.quality["price"] == FieldQualityState.CONFLICTING
    assert selected.field_values["price"].quality == FieldQualityState.CONFLICTING
