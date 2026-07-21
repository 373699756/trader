"""Unit tests for shared market-data normalization helpers."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from trader.infrastructure.market_data.normalize import (
    MarketQuoteInput,
    build_market_quote,
    infer_one_price_limit,
    normalize_quotes,
    to_float,
)
from trader.infrastructure.market_data.observations import SourceObservation


def test_to_float_handles_empty_and_invalid_numbers() -> None:
    assert to_float(None) is None
    assert to_float("") is None
    assert to_float("  ") is None
    assert to_float("nan") is None
    assert to_float("NaN") is None
    assert to_float("inf") is None
    assert to_float("-inf") is None


def test_to_float_keeps_finite_numbers() -> None:
    assert to_float("12.34") == 12.34
    assert to_float(12) == 12.0
    assert to_float(" 8 ") == 8.0
    assert to_float("-0.5") == -0.5


def _sample_row_row(code: str) -> dict[str, object]:
    return {
        "code": code,
        "name": f"{code} 样本",
        "price": "12.3",
        "previous_close": "11.8",
        "open_price": "11.9",
        "high": "12.5",
        "low": "11.7",
        "pct_change": "1.2",
        "change_5m": "0.3",
        "speed": "0.4",
        "volume_ratio": "2.1",
        "turnover_rate": "3.1",
        "amount": "450000000",
        "amplitude": "3.2",
        "market_cap": "1000000000",
        "industry": "金融",
        "is_st": False,
        "is_suspended": False,
    }


def _build_quote_from_row(row: dict[str, object], received_at: datetime):
    return build_market_quote(
        MarketQuoteInput(
            code=str(row["code"]),
            name=str(row["name"]),
            price=to_float(row.get("price")),
            previous_close=to_float(row.get("previous_close")),
            open_price=to_float(row.get("open_price")),
            high=to_float(row.get("high")),
            low=to_float(row.get("low")),
            pct_change=to_float(row.get("pct_change")),
            change_5m=to_float(row.get("change_5m")),
            speed=to_float(row.get("speed")),
            volume_ratio=to_float(row.get("volume_ratio")),
            turnover_rate=to_float(row.get("turnover_rate")),
            amount=to_float(row.get("amount")),
            amplitude=to_float(row.get("amplitude")),
            market_cap=to_float(row.get("market_cap")),
            industry=str(row.get("industry")),
            source="unit",
            source_time=received_at,
            received_time=received_at,
            data_version="normalize-test",
            is_st=bool(row.get("is_st")),
            is_suspended=bool(row.get("is_suspended")),
        )
    )


def _build_quote_input(
    now: datetime,
    *,
    code: str = "600001",
    source: str = "unit",
    data_version: str = "normalize-test",
    source_time: datetime | None = None,
) -> MarketQuoteInput:
    quote_time = source_time or now
    return MarketQuoteInput(
        code=code,
        name="600001 样本",
        price=12.3,
        previous_close=12.0,
        open_price=11.9,
        high=12.5,
        low=11.7,
        pct_change=1.2,
        change_5m=0.3,
        speed=0.4,
        volume_ratio=2.1,
        turnover_rate=3.1,
        amount=450000000,
        amplitude=3.2,
        market_cap=1000000000,
        industry="金融",
        source=source,
        source_time=quote_time,
        received_time=now,
        data_version=data_version,
        is_st=False,
        is_suspended=False,
    )


def test_normalize_quotes_drops_none_rows_and_accepts_iterators() -> None:
    received_at = datetime(2026, 7, 20, tzinfo=timezone.utc)

    def normalizer(row: dict[str, object], now: datetime):
        if row.get("code") == "600002":
            return None
        return _build_quote_from_row(row, now)

    rows = (_sample_row_row(code) for code in ("600001", "600002", "600003"))

    normalized = normalize_quotes(rows, received_at=received_at, normalizer=normalizer)

    assert [quote.code for quote in normalized] == ["600001", "600003"]
    assert len(normalized) == 2


def test_normalize_quotes_rejects_row_with_nonfinite_fields() -> None:
    received_at = datetime(2026, 7, 20, tzinfo=timezone.utc)

    def normalizer(row: dict[str, object], now: datetime):
        row = dict(row)
        if row["code"] == "600002":
            row["price"] = "nan"
        if row["code"] == "600003":
            row["amount"] = "inf"

        quote = _build_quote_from_row(row, now)
        if quote.price is None or quote.amount is None:
            return None
        return quote

    rows = [dict(_sample_row_row("600001")), dict(_sample_row_row("600002")), dict(_sample_row_row("600003"))]

    normalized = normalize_quotes(rows, received_at=received_at, normalizer=normalizer)

    assert [quote.code for quote in normalized] == ["600001"]


def test_normalize_quotes_skips_invalid_quote_input() -> None:
    received_at = datetime(2026, 7, 20, tzinfo=timezone.utc)

    def normalizer(row: dict[str, object], now: datetime):
        if row["code"] == "600002":
            return build_market_quote(
                MarketQuoteInput(
                    code="bad-code",
                    name="bad",
                    price=12.3,
                    previous_close=12.0,
                    open_price=11.9,
                    high=12.5,
                    low=11.7,
                    pct_change=1.2,
                    change_5m=None,
                    speed=None,
                    volume_ratio=None,
                    turnover_rate=None,
                    amount=450000000,
                    amplitude=3.2,
                    market_cap=1000000000,
                    industry="金融",
                    source="unit",
                    source_time=now,
                    received_time=now,
                    data_version="normalize-test",
                )
            )
        return _build_quote_from_row(row, now)

    rows = [_sample_row_row("600001"), _sample_row_row("600002"), _sample_row_row("600003")]

    normalized = normalize_quotes(rows, received_at=received_at, normalizer=normalizer)

    assert [quote.code for quote in normalized] == ["600001", "600003"]


def test_market_quote_input_rejects_invalid_code_source_version_and_naive_time() -> None:
    received_at = datetime(2026, 7, 20, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="6-digit"):
        build_market_quote(_build_quote_input(received_at, code="600"))

    with pytest.raises(ValueError, match="must not be empty"):
        build_market_quote(_build_quote_input(received_at, source=""))

    with pytest.raises(ValueError, match="must not be empty"):
        build_market_quote(_build_quote_input(received_at, data_version=""))

    with pytest.raises(ValueError, match="timezone-aware"):
        build_market_quote(_build_quote_input(datetime(2026, 7, 20), source_time=datetime(2026, 7, 20)))


@pytest.mark.parametrize(
    ("code", "pct_change", "expected"),
    [
        ("600001", 9.5, True),
        ("600001", -9.5, True),
        ("300001", 19.5, True),
        ("688001", -19.5, True),
        ("600001", 9.49, False),
        ("300001", 19.49, False),
    ],
)
def test_one_price_limit_is_inferred_at_board_specific_boundaries(
    code: str,
    pct_change: float,
    expected: bool,
) -> None:
    assert infer_one_price_limit(code, 12.0, 12.0, 12.0, pct_change) is expected


def test_one_price_limit_requires_a_finite_flat_price_range() -> None:
    assert infer_one_price_limit("600001", 12.0, 12.1, 12.0, 10.0) is False
    assert infer_one_price_limit("600001", 12.0, 12.0, 12.0, float("nan")) is False


def test_source_observation_copies_mappings_and_rejects_naive_time() -> None:
    now = datetime(2026, 7, 20, tzinfo=timezone.utc)
    fields = {"price": 12.0}
    missing = {"industry": "source_missing"}
    observation = SourceObservation(
        source="eastmoney",
        subject_key="600001",
        observed_at=now,
        source_time=now,
        received_at=now,
        effective_at=now,
        data_version="east-v1",
        fields=fields,
        missing_reasons=missing,
        payload_hash="payload-v1",
        status="success",
        error_code=None,
    )
    fields["price"] = 99.0
    missing["industry"] = "changed"

    assert observation.fields == {"price": 12.0}
    assert observation.missing_reasons == {"industry": "source_missing"}
    with pytest.raises(TypeError):
        observation.fields["price"] = 11.0

    with pytest.raises(ValueError, match="timezone-aware"):
        SourceObservation(
            source="eastmoney",
            subject_key="600001",
            observed_at=datetime(2026, 7, 20),
            source_time=now,
            received_at=now,
            effective_at=now,
            data_version="east-v1",
            fields={},
            missing_reasons={},
            payload_hash="payload-v1",
            status="success",
            error_code=None,
        )
