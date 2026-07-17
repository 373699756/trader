from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from trader.application.recommendations import RecommendationEngine
from trader.domain.models import FeatureSnapshot, Strategy


def test_targeted_quotes_are_hard_filtered_again_before_review_and_scoring(
    recommendation_policy,
    application_feature_factory,
) -> None:
    now = datetime.fromisoformat("2026-07-16T10:00:00+08:00")
    valid = application_feature_factory("600001", now)
    newly_too_hot = application_feature_factory("600002", now)
    newly_too_hot = replace(newly_too_hot, quote=replace(newly_too_hot.quote, pct_change=8.01))
    reviewer = RecordingReviewer()

    snapshot = RecommendationEngine(recommendation_policy).build_snapshot(
        Strategy.TODAY,
        (valid, newly_too_hot),
        now=now,
        phase="today_main",
        trade_date="2026-07-16",
        data_version="targeted-v2",
        review_port=reviewer,
        review_deadline=datetime.fromisoformat("2026-07-16T11:20:00+08:00"),
        max_age_seconds=20.0,
        filtered_count=0,
        filter_reasons={},
    )

    assert reviewer.reviewed_codes == ("600001",)
    assert [item.features.quote.code for item in snapshot.recommendations] == ["600001"]
    assert snapshot.filtered_count == 1
    assert snapshot.filter_reasons == {"main_board_too_hot": 1}


def test_snapshot_returns_zero_recommendations_instead_of_lowering_threshold(
    recommendation_policy,
    application_feature_factory,
) -> None:
    now = datetime.fromisoformat("2026-07-16T10:00:00+08:00")
    low = application_feature_factory("600001", now)
    low = replace(
        low,
        quote=replace(
            low.quote,
            pct_change=-1.0,
            change_5m=0.0,
            volume_ratio=0.8,
            turnover_rate=0.5,
        ),
        values={name: (200_000_000.0 if name == "amount_median_20d" else 0.0) for name in low.values},
    )

    snapshot = RecommendationEngine(recommendation_policy).build_snapshot(
        Strategy.TODAY,
        (low,),
        now=now,
        phase="today_main",
        trade_date="2026-07-16",
        data_version="below-threshold",
        review_port=None,
        review_deadline=datetime.fromisoformat("2026-07-16T11:20:00+08:00"),
        max_age_seconds=20.0,
        filtered_count=0,
        filter_reasons={},
    )

    assert snapshot.recommendations == ()


class RecordingReviewer:
    def __init__(self) -> None:
        self.reviewed_codes: tuple[str, ...] = ()

    def review(
        self,
        _strategy: Strategy,
        candidates: tuple[FeatureSnapshot, ...],
        *,
        phase: str,
        deadline: datetime,
    ) -> dict[str, object]:
        del phase, deadline
        self.reviewed_codes = tuple(candidate.quote.code for candidate in candidates)
        return {}
