from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from polars.exceptions import ColumnNotFoundError

from trader.application.youhua_test_doubles import P4ConsumerStub
from trader.domain.market.models import Board, CanonicalMarketSnapshot, Evidence, FeatureSnapshot, MarketQuote
from trader.domain.market.research import ResearchObservation
from trader.infra.market_data.columnar import (
    ColumnarFeatureBatch,
    ColumnarFeatureBatchOptions,
    ColumnarQuoteBatch,
    ColumnarResearchBatch,
    FeatureEnvelopeOptions,
    market_changes,
)

NOW = datetime(2026, 7, 22, 6, 49, 50, tzinfo=timezone.utc)


def _quote(
    code: str,
    price: float,
    version: str,
    *,
    board: Board = Board.MAIN,
    industry: str = "工业",
    has_major_regulatory_risk: bool = False,
) -> MarketQuote:
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
        industry=industry,
        source="fixture",
        source_time=NOW,
        received_time=NOW,
        data_version=version,
        has_major_regulatory_risk=has_major_regulatory_risk,
        board=board,
    )


def _snapshot(epoch: str, quotes: tuple[MarketQuote, ...]) -> CanonicalMarketSnapshot:
    return CanonicalMarketSnapshot(NOW, epoch, tuple(sorted(quotes, key=lambda quote: quote.code)), {}, {}, (), {}, ())


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
    assert changes.schema_version == "market_change_set_v1"
    assert changes.previous_merge_epoch == "epoch-1"
    assert previous.identity.digest != current.identity.digest


def test_columnar_change_set_reports_dimensions_field_families_and_risk_changes() -> None:
    previous = ColumnarQuoteBatch.from_snapshot(
        _snapshot(
            "epoch-1",
            (
                _quote("600001", 10.0, "v1", board=Board.MAIN, industry="工业"),
                _quote("300001", 20.0, "v1", board=Board.CHINEXT, industry="医药"),
            ),
        ),
        config_version="config-v17",
        schema_version="schema-v6",
    )
    current = ColumnarQuoteBatch.from_snapshot(
        _snapshot(
            "epoch-2",
            (
                replace(
                    _quote("600001", 11.0, "v2", board=Board.MAIN, industry="工业"),
                    amount=1_200_000.0,
                ),
                _quote(
                    "300001",
                    20.0,
                    "v1",
                    board=Board.CHINEXT,
                    industry="医药",
                    has_major_regulatory_risk=True,
                ),
            ),
        ),
        config_version="config-v17",
        schema_version="schema-v6",
    )

    changes = market_changes(previous, current)

    assert changes.updated_codes == ("300001", "600001")
    assert changes.dirty_boards == ("chinext", "main")
    assert changes.dirty_industries == ("医药", "工业")
    assert changes.dirty_field_families == ("quote_identity", "quote_liquidity", "quote_price", "risk")
    assert changes.risk_changed_codes == ("300001",)
    assert changes.overlay_only is False
    assert changes.full_invalidation_reason is None
    assert changes.content_hash == current.identity.content_hash


def test_columnar_quote_price_tick_is_overlay_only() -> None:
    previous = ColumnarQuoteBatch.from_snapshot(
        _snapshot("epoch-1", (_quote("600001", 10.0, "v1"),)),
        config_version="config-v17",
        schema_version="schema-v6",
    )
    current = ColumnarQuoteBatch.from_snapshot(
        _snapshot("epoch-2", (replace(_quote("600001", 10.2, "v1"), pct_change=2.0),)),
        config_version="config-v17",
        schema_version="schema-v6",
    )

    changes = market_changes(previous, current)

    assert changes.updated_codes == ("600001",)
    assert changes.dirty_field_families == ("quote_price",)
    assert changes.overlay_only is True


def test_columnar_batches_reject_object_columns_and_record_manifest_hashes() -> None:
    evidence = Evidence(
        evidence_id="news-1",
        evidence_type="news",
        title="订单增长",
        source="fixture",
        published_at=NOW,
        data_version="evidence-v1",
    )
    research = ColumnarResearchBatch.from_observations(
        {
            "600001": ResearchObservation(
                announcements_available=True,
                pledge_ratio_pct=1.5,
                unlock_ratio_pct=None,
                evidence=(evidence,),
            )
        },
        merge_epoch="epoch-r",
        config_version="config-v17",
    )
    feature = FeatureSnapshot(
        quote=_quote("600001", 10.0, "v1"),
        values={"speed_percentile": 88.0, "industry_strength": None},
        observed_at=NOW + timedelta(seconds=1),
        history_days=20,
        evidence=(evidence,),
        market_regime="risk_on",
        competition_group_id="工业:设备",
        liquidity_bucket="p80",
    )
    features = ColumnarFeatureBatch.from_features(
        (feature,),
        ColumnarFeatureBatchOptions(
            merge_epoch="epoch-f",
            config_version="config-v17",
            feature_schema_version="feature-schema-v1",
            feature_names=("speed_percentile", "industry_strength"),
            strategy_version="strategy-v1",
            board_policy_version="board-v1",
        ),
    )

    assert research.identity.manifest_hash
    assert features.identity.manifest_hash
    assert features.frame.schema["speed_percentile"].base_type().__name__ == "Float64"
    assert features.frame.schema["industry_strength"].base_type().__name__ == "Float64"
    assert "Object" not in {dtype.base_type().__name__ for dtype in features.frame.schema.values()}


def test_schema_or_config_change_expands_to_full_invalidation_reason() -> None:
    previous = ColumnarQuoteBatch.from_snapshot(
        _snapshot("epoch-1", (_quote("600001", 10.0, "v1"),)),
        config_version="config-v17",
        schema_version="schema-v6",
    )
    current = ColumnarQuoteBatch.from_snapshot(
        _snapshot("epoch-2", (_quote("600001", 10.0, "v1"),)),
        config_version="config-v18",
        schema_version="schema-v6",
    )

    changes = market_changes(previous, current)

    assert changes.full_invalidation_reason == "config_version_changed"
    assert changes.has_full_invalidation is True


def test_market_changes_requires_strict_quote_schema() -> None:
    previous = ColumnarQuoteBatch.from_snapshot(
        _snapshot("epoch-1", (_quote("600001", 10.0, "v1"),)),
        config_version="config-v17",
        schema_version="schema-v6",
    )
    current = ColumnarQuoteBatch.from_snapshot(
        _snapshot("epoch-2", (_quote("600001", 10.0, "v1"),)),
        config_version="config-v17",
        schema_version="schema-v6",
    )
    broken = replace(current, frame=current.frame.drop("source_time"))

    with pytest.raises(ColumnNotFoundError):
        market_changes(previous, broken)


def test_feature_batch_builds_a2_public_envelope_for_p4_stub() -> None:
    feature = FeatureSnapshot(
        quote=_quote("600001", 10.0, "v1"),
        values={"speed_percentile": 88.0},
        observed_at=NOW,
        history_days=20,
    )
    batch = ColumnarFeatureBatch.from_features(
        (feature,),
        ColumnarFeatureBatchOptions(
            merge_epoch="epoch-public",
            config_version="config-v17",
            feature_schema_version="feature-schema-v1",
            feature_names=("speed_percentile",),
        ),
    )
    change_set = market_changes(
        None,
        ColumnarQuoteBatch.from_snapshot(
            _snapshot("epoch-public", (_quote("600001", 10.0, "v1"),)),
            config_version="config-v17",
            schema_version="schema-v6",
        ),
    )

    envelope = batch.to_public_envelope(
        (feature,),
        change_set,
        FeatureEnvelopeOptions(
            trade_date="2026-07-22",
            phase="today",
            data_version="features-v1",
        ),
    )
    receipt = P4ConsumerStub().consume(envelope)

    assert receipt.merge_epoch == "epoch-public"
    assert receipt.content_hash == batch.identity.content_hash
    assert receipt.feature_count == 1
    assert receipt.changed_codes == ("600001",)
    assert "quote_price" in receipt.dirty_field_families
