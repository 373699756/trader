from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

import pytest

from tests.unit.application.research.test_historical_ports import OBSERVED_AT, TRADE_DATE, _bundle, _summary
from trader.application.research.tomorrow_feature_models import (
    TomorrowFeatureContext,
    TomorrowFeatureContextBatch,
)
from trader.application.research.tomorrow_features import ScoreTomorrowPointInTimeFeatures
from trader.domain.research.tomorrow_features import PointInTimePublishedFact


def _contexts() -> TomorrowFeatureContextBatch:
    summary = _summary()
    board_by_code = {item.code: item.board for item in summary.candidates}
    return TomorrowFeatureContextBatch(
        trade_date=TRADE_DATE,
        input_hash=summary.input_hash,
        contexts=tuple(
            TomorrowFeatureContext(
                code=code,
                board=board_by_code[code],
                industry="unknown",
                industry_effective_at=OBSERVED_AT - timedelta(days=30),
                industry_received_at=OBSERVED_AT - timedelta(days=20),
                observed_at=OBSERVED_AT,
                current_open=10.4,
                current_high=10.8,
                current_low=10.2,
                current_last=10.6,
                market_cap=1_000_000_000.0,
                liquidity=20_000_000.0,
                published_facts=(
                    PointInTimePublishedFact(
                        kind="financial",
                        name="earnings_revision",
                        value=0.2,
                        report_period=date(2026, 6, 30),
                        published_at=OBSERVED_AT - timedelta(days=5),
                        received_at=OBSERVED_AT - timedelta(days=4),
                        source="cninfo",
                        evidence_hash="b" * 64,
                    ),
                ),
            )
            for code in ("300001", "600001", "688001")
        ),
    )


def test_feature_batch_binds_r2_identity_and_has_stable_content_hash() -> None:
    service = ScoreTomorrowPointInTimeFeatures()

    first = service.build(_summary(), _bundle(("300001", "600001", "688001")), _contexts())
    second = service.build(_summary(), _bundle(("300001", "600001", "688001")), _contexts())

    assert first == second
    assert first.schema_version == "score_tomorrow_point_in_time_features_v1"
    assert first.input_hash == _summary().input_hash
    assert first.context_hash == _contexts().content_hash
    assert len(first.content_hash) == 64
    assert tuple(row.code for row in first.rows) == ("300001", "600001", "688001")
    assert first.production_authority is False


def test_feature_batch_rejects_r2_identity_and_industry_mismatches() -> None:
    service = ScoreTomorrowPointInTimeFeatures()
    bundle = _bundle(("300001", "600001", "688001"))

    with pytest.raises(ValueError, match="input hash"):
        service.build(_summary(), bundle, replace(_contexts(), input_hash="f" * 64))
    first = _contexts().contexts[0]
    mismatched = replace(_contexts(), contexts=(replace(first, industry="future-industry"), *_contexts().contexts[1:]))
    with pytest.raises(ValueError, match="industry"):
        service.build(_summary(), bundle, mismatched)


def test_feature_context_rejects_future_industry_and_disclosure_times() -> None:
    context = _contexts().contexts[0]

    with pytest.raises(ValueError, match="industry.*cutoff"):
        replace(context, industry_effective_at=OBSERVED_AT + timedelta(seconds=1))
    future_fact = replace(
        context.published_facts[0],
        report_period=date(2025, 12, 31),
        published_at=OBSERVED_AT + timedelta(seconds=1),
        received_at=OBSERVED_AT + timedelta(seconds=1),
    )
    with pytest.raises(ValueError, match="published after feature cutoff"):
        replace(context, published_facts=(future_fact,))
