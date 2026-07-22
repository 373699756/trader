from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

from trader.domain.market.models import CanonicalMarketSnapshot, MarketQuote
from trader.infra.market_data.columnar import ColumnarQuoteBatch, market_changes

NOW = datetime(2026, 7, 22, 6, 49, 50, tzinfo=timezone.utc)


def _quote(code: str, price: float, version: str) -> MarketQuote:
    return MarketQuote(
        code=code,
        name=code,
        price=price,
        previous_close=price - 1,
        open_price=price,
        high=price,
        low=price,
        pct_change=1.0,
        change_5m=0.1,
        speed=0.1,
        volume_ratio=1.2,
        turnover_rate=2.0,
        amount=1_000_000.0,
        amplitude=2.0,
        market_cap=10_000_000.0,
        industry="工业",
        source="fixture",
        source_time=NOW,
        received_time=NOW,
        data_version=version,
    )


def _snapshot(epoch: str, quotes: tuple[MarketQuote, ...]) -> CanonicalMarketSnapshot:
    return CanonicalMarketSnapshot(NOW, epoch, quotes, {}, {}, (), {}, ())


def test_columnar_market_change_set_is_deterministic_and_identity_is_versioned() -> None:
    previous = ColumnarQuoteBatch.from_snapshot(
        _snapshot("epoch-1", (_quote("600001", 10.0, "v1"), _quote("600002", 20.0, "v1"))),
        config_version="config-v17",
        schema_version="schema-v6",
    )
    current = ColumnarQuoteBatch.from_snapshot(
        _snapshot(
            "epoch-2",
            (
                replace(_quote("600001", 11.0, "v2"), pct_change=2.0),
                _quote("600003", 30.0, "v1"),
            ),
        ),
        config_version="config-v17",
        schema_version="schema-v6",
    )

    changes = market_changes(previous, current)

    assert changes.inserted_codes == ("600003",)
    assert changes.updated_codes == ("600001",)
    assert changes.removed_codes == ("600002",)
    assert changes.dirty_codes == ("600001", "600002", "600003")
    assert previous.identity.digest != current.identity.digest
