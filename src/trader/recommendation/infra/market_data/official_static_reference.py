"""Parse the supplier boundary into one immutable official issuer population."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date

from trader.infra.market_data.observations import SourceObservation
from trader.recommendation.application.runtime.schedule import SHANGHAI
from trader.recommendation.domain.market.models import Board
from trader.recommendation.domain.market.static import StaticIssuer, StaticMarketReference


def parse_official_static_reference(observations: Sequence[SourceObservation]) -> StaticMarketReference:
    """Accept only the full response already validated by the official client."""
    if not observations:
        raise ValueError("official static population must not be empty")
    records: list[StaticIssuer] = []
    versions = {item.data_version for item in observations}
    timestamps = {item.source_time for item in observations}
    if len(versions) != 1 or not all(versions) or len(timestamps) != 1:
        raise ValueError("official static population must have one identity and source time")
    for item in observations:
        name = item.fields.get("name")
        board = item.fields.get("board")
        exchange = item.fields.get("exchange")
        listing_raw = item.fields.get("listing_date")
        if (
            item.source != "exchange_security_master"
            or item.status != "success"
            or len(item.subject_key) != 6
            or not item.subject_key.isascii()
            or not item.subject_key.isdigit()
            or not isinstance(name, str)
            or not name.strip()
            or board not in {"main", "chinext", "star"}
            or exchange not in {"SSE", "SZSE"}
            or not isinstance(listing_raw, str)
            or item.effective_at > item.observed_at
            or item.source_time.tzinfo is None
            or item.source_time.utcoffset() is None
        ):
            raise ValueError("official static issuer is invalid")
        listing_date = date.fromisoformat(listing_raw)
        if listing_date > item.observed_at.astimezone(SHANGHAI).date():
            raise ValueError("official static issuer listing is in the future")
        records.append(
            StaticIssuer(item.subject_key, name.strip(), Board(board), str(exchange), listing_date, False, False)
        )
    if len({item.code for item in records}) != len(records):
        raise ValueError("official static issuer codes must be unique")
    return StaticMarketReference(tuple(sorted(records, key=lambda item: item.code)), versions.pop(), timestamps.pop())
