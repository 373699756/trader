from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import cast

import pytest

from trader.domain.market.models import MarketQuote
from trader.infra.market_data.provider_adapter import (
    ProviderQuery,
    ProviderRawPayload,
    run_columnar_provider_adapter,
)

NOW = datetime(2026, 7, 22, 7, 30, tzinfo=timezone.utc)


class FakeColumnarAdapter:
    source = "fixture"

    def transform_query(self, query: ProviderQuery) -> ProviderQuery:
        return replace(query, requested_fields=tuple(sorted(query.requested_fields)))

    def extract_data(self, query: ProviderQuery) -> ProviderRawPayload:
        return ProviderRawPayload(
            query=query,
            rows=({"code": "600001", "price": 10.0}, {"code": "600002", "price": None}),
            received_at=NOW,
            lineage={"source": self.source, "query": query.identity_hash},
            missing_reasons={"600002.price": "provider_null"},
        )

    def transform_data(self, payload: ProviderRawPayload) -> tuple[MarketQuote, ...]:
        return tuple(
            MarketQuote(
                code=str(row["code"]),
                name=str(row["code"]),
                price=cast(float | None, row["price"]),
                previous_close=9.8,
                open_price=9.9,
                high=10.1,
                low=9.7,
                pct_change=2.0,
                change_5m=None,
                speed=None,
                volume_ratio=None,
                turnover_rate=2.0,
                amount=100_000_000.0,
                amplitude=None,
                market_cap=None,
                industry="工业",
                source=self.source,
                source_time=payload.received_at,
                received_time=payload.received_at,
                data_version="fixture-v1",
            )
            for row in payload.rows
        )


def _query() -> ProviderQuery:
    return ProviderQuery(
        dataset="full_market_quotes",
        source="fixture",
        subject_key="ashare",
        requested_fields=("price", "code"),
        requested_at=NOW,
        deadline_at=NOW + timedelta(seconds=1),
        source_contract_version="fixture-v1",
    )


def test_provider_adapter_runs_query_extract_transform_into_columnar_batch() -> None:
    result = run_columnar_provider_adapter(
        FakeColumnarAdapter(),
        _query(),
        merge_epoch="epoch-provider",
        config_version="config-v17",
        schema_version="schema-v6",
    )

    assert result.query.requested_fields == ("code", "price")
    assert result.batch.frame.get_column("code").to_list() == ["600001", "600002"]
    assert result.batch.identity.content_hash
    assert result.batch.identity.manifest_hash == result.lineage_hash
    assert result.missing_reasons == {"600002.price": "provider_null"}


def test_provider_query_rejects_naive_time_and_past_deadline() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        ProviderQuery(
            dataset="full_market_quotes",
            source="fixture",
            subject_key="ashare",
            requested_fields=("price",),
            requested_at=datetime(2026, 7, 22),
            deadline_at=None,
            source_contract_version="fixture-v1",
        )

    with pytest.raises(ValueError, match="deadline"):
        replace(_query(), deadline_at=NOW - timedelta(seconds=1))


def test_provider_adapter_rejects_non_finite_quote_values() -> None:
    class BadAdapter(FakeColumnarAdapter):
        def transform_data(self, payload: ProviderRawPayload) -> tuple[MarketQuote, ...]:
            quote = super().transform_data(payload)[0]
            return (replace(quote, price=float("nan")),)

    with pytest.raises(ValueError, match="non-finite"):
        run_columnar_provider_adapter(
            BadAdapter(),
            _query(),
            merge_epoch="epoch-provider",
            config_version="config-v17",
            schema_version="schema-v6",
        )
