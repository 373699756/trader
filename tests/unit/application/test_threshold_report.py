from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from trader.application.recommendations import RecommendationEngine
from trader.application.threshold_report import build_threshold_report
from trader.domain.models import Strategy


def test_threshold_report_uses_all_replayed_candidates_and_required_metrics(
    recommendation_policy,
    application_feature_factory,
) -> None:
    engine = RecommendationEngine(recommendation_policy)
    first_at = datetime.fromisoformat("2026-07-16T10:00:00+08:00")
    first = _frozen_snapshot(engine, recommendation_policy, application_feature_factory, first_at, "600001")
    second = _frozen_snapshot(
        engine,
        recommendation_policy,
        application_feature_factory,
        first_at + timedelta(minutes=10),
        "600003",
    )

    report = build_threshold_report((first, second))
    today = report["strategies"]["today"]

    assert report["schema_version"] == "threshold_report_v1"
    assert today["snapshot_count"] == 2
    assert today["candidate_count"] == 4
    assert today["score_distribution"]["count"] == 4
    assert today["recommendation_count"]["total"] == 2
    assert today["empty_recommendation_ratio"] == 0.0
    assert today["topk_change"]["comparison_count"] == 1
    assert today["topk_change"]["mean_jaccard_distance"] == 1.0
    assert today["deepseek_coverage"]["applied_ratio"] == 0.0
    assert today["local_degraded_ratio"] == 1.0
    assert today["risk_block_rate"] == 0.0

    with pytest.raises(ValueError, match="mixed strategy versions"):
        build_threshold_report((first, replace(second, strategy_version="other")))


def _frozen_snapshot(engine, policy, feature_factory, now: datetime, selected_code: str):
    high = feature_factory(selected_code, now)
    low = feature_factory("600002", now)
    low = replace(
        low,
        quote=replace(low.quote, pct_change=-1.0, change_5m=0.0, volume_ratio=0.8, turnover_rate=0.5),
        values={name: (200_000_000.0 if name == "amount_median_20d" else 0.0) for name in low.values},
    )
    market = (high, low)
    candidates, reasons, details = engine.preselect(market, now=now, max_age_seconds=20.0, limit=120)
    snapshot = engine.build_snapshot(
        Strategy.TODAY,
        candidates,
        now=now,
        phase="today_main",
        trade_date="2026-07-16",
        data_version=f"report:{now.isoformat()}",
        review_port=None,
        review_deadline=now + timedelta(hours=1),
        max_age_seconds=20.0,
        filtered_count=0,
        filter_reasons=reasons,
        filter_details=details,
        market_features=market,
        requested_codes=tuple(feature.quote.code for feature in candidates),
        preselect_max_age_seconds=20.0,
        candidate_pool_size=120,
    )
    assert policy.selection.default_top_k == 10
    return replace(snapshot, frozen=True)
