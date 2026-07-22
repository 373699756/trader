"""B-internal provider adapter contracts for columnar P1 ingestion."""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Protocol

from trader.application.cache import canonical_json_bytes
from trader.domain.market.models import MarketQuote
from trader.infra.market_data.columnar import ColumnarQuoteBatch


@dataclass(frozen=True)
class ProviderQuery:
    dataset: str
    source: str
    subject_key: str
    requested_fields: tuple[str, ...]
    requested_at: datetime
    deadline_at: datetime | None
    source_contract_version: str

    def __post_init__(self) -> None:
        for value, name in ((self.requested_at, "requested_at"), (self.deadline_at, "deadline_at")):
            if value is not None and (value.tzinfo is None or value.utcoffset() is None):
                raise ValueError(f"provider query {name} must be timezone-aware")
        if self.deadline_at is not None and self.deadline_at < self.requested_at:
            raise ValueError("provider query deadline must not be before requested_at")
        if not all((self.dataset.strip(), self.source.strip(), self.subject_key.strip(), self.source_contract_version)):
            raise ValueError("provider query identity fields must not be empty")
        if not self.requested_fields or any(not field_name.strip() for field_name in self.requested_fields):
            raise ValueError("provider query requested_fields must be non-empty")

    @property
    def identity_hash(self) -> str:
        return hashlib.sha256(canonical_json_bytes(self.__dict__)).hexdigest()


@dataclass(frozen=True)
class ProviderRawPayload:
    query: ProviderQuery
    rows: tuple[Mapping[str, object], ...]
    received_at: datetime
    lineage: Mapping[str, str] = field(default_factory=dict)
    missing_reasons: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.received_at.tzinfo is None or self.received_at.utcoffset() is None:
            raise ValueError("provider payload received_at must be timezone-aware")
        object.__setattr__(self, "lineage", MappingProxyType(dict(self.lineage)))
        object.__setattr__(self, "missing_reasons", MappingProxyType(dict(self.missing_reasons)))
        if any(value is None for value in self.missing_reasons.values()):
            raise ValueError("provider missing reasons must be explicit strings; missing data stays null")


@dataclass(frozen=True)
class ProviderColumnarResult:
    query: ProviderQuery
    batch: ColumnarQuoteBatch
    missing_reasons: Mapping[str, str]
    lineage_hash: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "missing_reasons", MappingProxyType(dict(self.missing_reasons)))


class ColumnarProviderAdapter(Protocol):
    source: str

    def transform_query(self, query: ProviderQuery) -> ProviderQuery: ...

    def extract_data(self, query: ProviderQuery) -> ProviderRawPayload: ...

    def transform_data(self, payload: ProviderRawPayload) -> tuple[MarketQuote, ...]: ...


def run_columnar_provider_adapter(
    adapter: ColumnarProviderAdapter,
    query: ProviderQuery,
    *,
    merge_epoch: str,
    config_version: str,
    schema_version: str,
) -> ProviderColumnarResult:
    transformed_query = adapter.transform_query(query)
    payload = adapter.extract_data(transformed_query)
    if payload.query != transformed_query:
        raise ValueError("provider payload query lineage does not match transformed query")
    quotes = adapter.transform_data(payload)
    _validate_quotes(quotes)
    batch = ColumnarQuoteBatch.from_quotes(
        quotes,
        merge_epoch=merge_epoch,
        config_version=config_version,
        schema_version=schema_version,
        manifest_hash=_lineage_hash(payload.lineage),
    )
    return ProviderColumnarResult(
        query=transformed_query,
        batch=batch,
        missing_reasons=payload.missing_reasons,
        lineage_hash=_lineage_hash(payload.lineage),
    )


def _validate_quotes(quotes: Sequence[MarketQuote]) -> None:
    for quote in quotes:
        if len(quote.code) != 6 or not quote.code.isdigit():
            raise ValueError("provider adapter produced an invalid quote code")
        if not quote.data_version.strip():
            raise ValueError("provider adapter produced an empty data_version")
        for time_value, name in ((quote.source_time, "source_time"), (quote.received_time, "received_time")):
            if time_value.tzinfo is None or time_value.utcoffset() is None:
                raise ValueError(f"provider adapter produced naive {name}")
        for numeric_value in (
            quote.price,
            quote.pct_change,
            quote.amount,
            quote.turnover_rate,
            quote.cross_source_deviation_pct,
        ):
            if numeric_value is not None and not math.isfinite(numeric_value):
                raise ValueError("provider adapter produced a non-finite numeric value")


def _lineage_hash(lineage: Mapping[str, str]) -> str:
    return hashlib.sha256(canonical_json_bytes(dict(lineage))).hexdigest()


__all__ = [
    "ColumnarProviderAdapter",
    "ProviderColumnarResult",
    "ProviderQuery",
    "ProviderRawPayload",
    "run_columnar_provider_adapter",
]
