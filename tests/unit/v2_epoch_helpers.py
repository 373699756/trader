from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from datetime import date, datetime

from trader.domain.market.epochs import DataPlaneCoverage
from trader.domain.market.models import Board, LiveQuote, MarketQuote
from trader.domain.market.quality import FieldQualityState, FieldValue


def field_values(
    values: Mapping[str, str | int | float | bool | date | Board | None],
    *,
    source: str,
    source_time: datetime,
    received_time: datetime,
    data_version: str,
) -> Mapping[str, FieldValue]:
    return {
        name: FieldValue(
            name=name,
            value=_field_scalar(value),
            source=source,
            source_time=source_time,
            received_time=received_time,
            data_version=data_version,
            payload_hash=hashlib.sha256(
                f"{name}|{value!r}|{source}|{source_time.isoformat()}|{data_version}".encode()
            ).hexdigest(),
            quality=FieldQualityState.MISSING if value is None else FieldQualityState.VALID,
        )
        for name, value in values.items()
    }


def daily_field_values(
    values: Mapping[str, float | None],
    *,
    source_time: datetime,
    received_time: datetime,
    data_version: str = "history-v1",
) -> Mapping[str, FieldValue]:
    return field_values(
        values,
        source="tencent",
        source_time=source_time,
        received_time=received_time,
        data_version=data_version,
    )


def market_field_values(quote: MarketQuote) -> Mapping[str, FieldValue]:
    values: dict[str, str | int | float | bool | None] = {
        "amount": quote.amount,
        "board": quote.board.value,
        "exchange": quote.exchange,
        "high": quote.high,
        "listing_age_sessions": quote.listing_age_sessions,
        "listing_date": quote.listing_date,
        "low": quote.low,
        "name": quote.name,
        "open_price": quote.open_price,
        "pct_change": quote.pct_change,
        "previous_close": quote.previous_close,
        "price": quote.price,
    }
    return field_values(
        values,
        source=quote.source,
        source_time=quote.source_time,
        received_time=quote.received_time,
        data_version=quote.data_version,
    )


def candidate_field_values(quote: LiveQuote) -> Mapping[str, FieldValue]:
    return field_values(
        {
            "cross_source_deviation_pct": quote.cross_source_deviation_pct,
            "cross_source_verified": quote.cross_source_verified,
            "pct_change": quote.pct_change,
            "price": quote.price,
        },
        source=quote.source,
        source_time=quote.source_time,
        received_time=quote.received_time,
        data_version=quote.data_version,
    )


def research_field_values(
    *,
    source_time: datetime,
    received_time: datetime,
    data_version: str,
) -> Mapping[str, FieldValue]:
    return field_values(
        {
            "announcements": "available",
            "corporate_risk": "unknown",
            "financial": "unknown",
            "pledge": "unknown",
            "unlock": "unknown",
        },
        source="issuer",
        source_time=source_time,
        received_time=received_time,
        data_version=data_version,
    )


def coverage(
    codes: Sequence[str],
    *,
    candidate_codes: Sequence[str] | None = None,
) -> DataPlaneCoverage:
    normalized = tuple(sorted(codes))
    candidates = tuple(sorted(candidate_codes if candidate_codes is not None else codes))
    return DataPlaneCoverage(
        potential_executable_codes=normalized,
        security_master_codes=normalized,
        candidate_codes=candidates,
        candidate_history_codes=candidates,
    )


def nested_market_field_values(quotes: Sequence[MarketQuote]) -> Mapping[str, Mapping[str, FieldValue]]:
    return {quote.code: market_field_values(quote) for quote in quotes}


def nested_candidate_field_values(quotes: Sequence[LiveQuote]) -> Mapping[str, Mapping[str, FieldValue]]:
    return {quote.code: candidate_field_values(quote) for quote in quotes}


def _field_scalar(value: str | int | float | bool | date | Board | None) -> str | float | bool | None:
    if isinstance(value, Board):
        return value.value
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, int) and not isinstance(value, bool):
        return float(value)
    return value
