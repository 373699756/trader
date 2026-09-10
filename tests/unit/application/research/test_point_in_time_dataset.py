from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from trader.application.research.point_in_time_dataset import (
    PointInTimeDatasetBuilder,
    PointInTimeDatasetBuildRequest,
    PointInTimeDaySource,
    PointInTimeSourceRow,
)
from trader.domain.market.feature_contracts import FeatureId, FeatureVectorManifest
from trader.domain.market.models import Board
from trader.domain.outcome.models import OutcomeBar, OutcomePrice, OutcomeTradingStatus
from trader.domain.recommendation.models import BoardStrategyPolicy, Strategy
from trader.domain.recommendation.selection.scored_selection import ScoredSelectionPolicy
from trader.domain.research.point_in_time_data_qualification import (
    DailyArchiveQualification,
    HistoricalIndustryQualification,
    HistoricalMinuteQualification,
    build_point_in_time_data_qualification,
)
from trader.domain.research.point_in_time_dataset import (
    POINT_IN_TIME_BENCHMARK_ID,
    PointInTimeDateSplit,
    PointInTimeEventFact,
    PointInTimeIndustryFact,
    PointInTimeSourceIdentity,
)
from trader.infra.research.point_in_time_dataset_artifacts import (
    PointInTimeDatasetArtifactConflictError,
    PointInTimeDatasetArtifactStore,
)

HASHES = tuple(character * 64 for character in "abcdef0123456789")


def _qualification(*, ready: bool = True):
    daily = DailyArchiveQualification(
        sessions=2000 if ready else 0,
        universe_count=3,
        completed_codes=3 if ready else 0,
        failed_codes=0,
        coverage_status="coverage_ready" if ready else "not_available",
        manifest_hash=HASHES[0] if ready else "",
        state="qualified" if ready else "historical_data_insufficient",
        failure_reasons=() if ready else ("daily_archive_unavailable",),
    )
    industry = HistoricalIndustryQualification(
        source="official_industry",
        sampled_codes=300 if ready else 0,
        required_sample_codes=300,
        code_available=ready,
        industry_available=ready,
        classification_available=ready,
        effective_from_available=ready,
        effective_to_available=ready,
        queried_at_available=ready,
        source_identity_available=ready,
        source_evidence_hash=HASHES[1] if ready else "",
        state="qualified" if ready else "historical_data_insufficient",
        failure_reasons=() if ready else ("industry_source_unavailable",),
    )
    minute = HistoricalMinuteQualification(
        source="official_minute",
        earliest_available=date(2020, 1, 1) if ready else None,
        sampled_codes=300 if ready else 0,
        matched_codes=300 if ready else 0,
        sampled_trade_dates=600 if ready else 0,
        matched_trade_dates=600 if ready else 0,
        coverage_ratio=1.0 if ready else 0.0,
        timezone="Asia/Shanghai" if ready else "",
        supports_1120=ready,
        supports_1450=ready,
        volume_available=ready,
        amount_available=ready,
        raw_qfq_pair_available=ready,
        corporate_action_semantics_proven=ready,
        source_evidence_hash=HASHES[2],
        state="qualified" if ready else "historical_data_insufficient",
        failure_reasons=() if ready else ("minute_source_unavailable",),
    )
    return build_point_in_time_data_qualification(daily, (industry,), (minute,))


def _split() -> PointInTimeDateSplit:
    start = date(2024, 1, 1)
    return PointInTimeDateSplit(
        training_dates=(start,),
        early_stopping_dates=(start + timedelta(days=1),),
        calibration_dates=(start + timedelta(days=2),),
        development_confirmation_embargo_dates=tuple(start + timedelta(days=3 + index) for index in range(5)),
        confirmation_dates=(start + timedelta(days=8),),
        confirmation_holdout_embargo_dates=tuple(start + timedelta(days=9 + index) for index in range(5)),
        terminal_holdout_dates=tuple(start + timedelta(days=14 + index) for index in range(200)),
    )


def _board_policy(board: Board) -> BoardStrategyPolicy:
    return BoardStrategyPolicy(
        policy_id=f"tomorrow-policy:{board.value}",
        version="tomorrow-policy",
        board=board,
        strategy=Strategy.TOMORROW,
        candidate_weights={"liquidity": 0.4, "trend": 0.3, "stability": 0.2, "data_completeness": 0.1},
        local_weights={
            "tail_structure": 0.2,
            "turnover_flow": 0.1,
            "trend": 0.2,
            "stability": 0.2,
            "market_state": 0.1,
            "entry_quality": 0.2,
        },
        candidate_component_weights={
            "stability": {"low_volatility_score": 0.5, "low_drawdown_score": 0.5},
        },
        local_component_weights={
            "tail_structure": {"tail_return_30m": 0.35, "tail_volume_ratio": 0.3, "close_location": 0.35},
            "turnover_flow": {
                "turnover_shock_score": 0.35,
                "amount_shock_score": 0.35,
                "flow_confirmation_score": 0.3,
            },
            "trend": {"ma20_60_position": 0.375, "ma_slope": 0.375, "breakout_20d": 0.25},
            "stability": {"low_volatility_score": 0.5, "low_drawdown_score": 0.5},
        },
        candidate_min_score=0.0,
    )


def _policy() -> ScoredSelectionPolicy:
    return ScoredSelectionPolicy(
        board_policies={board: _board_policy(board) for board in (Board.MAIN, Board.CHINEXT, Board.STAR)},
        risk_rules={},
        max_age_seconds=60.0,
        local_risk_cap=25.0,
        candidate_limit_per_board=1,
        top_k=1,
        maximum_per_industry=2,
    )


def _request(qualification) -> PointInTimeDatasetBuildRequest:
    return PointInTimeDatasetBuildRequest(
        qualification=qualification,
        date_split=_split(),
        feature_manifest=FeatureVectorManifest(
            (FeatureId("atr20_pct"), FeatureId("trend_score")),
            ("percentage_point", "score_0_100"),
            ("reject_model_input", "reject_model_input"),
            HASHES[3],
        ),
        calendar_hash=HASHES[4],
        security_master_hash=HASHES[5],
        selection_policy=_policy(),
    )


class _ForbiddenSource:
    def load_day(self, trade_date: date) -> PointInTimeDaySource:
        raise AssertionError(f"source must not be read: {trade_date}")


class _Source:
    def __init__(self, feature_factory) -> None:
        self.feature_factory = feature_factory
        self.loaded_dates: list[date] = []

    def load_day(self, trade_date: date) -> PointInTimeDaySource:
        self.loaded_dates.append(trade_date)
        shanghai = ZoneInfo("Asia/Shanghai")
        anchor = datetime.combine(trade_date, datetime.min.time(), tzinfo=shanghai).replace(hour=14, minute=50)
        exit_date = trade_date + timedelta(days=1)
        rows = []
        for index, (code, exit_qfq) in enumerate((("600000", 25.2), ("600001", 22.8), ("600002", 24.5))):
            feature = self.feature_factory(code, anchor, industry=f"industry-{index}")
            feature = replace(
                feature,
                quote=replace(
                    feature.quote,
                    board=Board.MAIN,
                    board_source="security_master",
                    board_reliability="verified",
                    listing_age_sessions=100,
                    is_st=code == "600002",
                ),
            )
            reference = OutcomeBar(
                trade_date.isoformat(),
                OutcomePrice(23.5, 24.2, 23.0, 24.0),
                OutcomePrice(11.8, 12.2, 11.7, 12.0),
                OutcomeTradingStatus.TRADABLE,
                "fixture",
            )
            exit_bar = OutcomeBar(
                exit_date.isoformat(),
                OutcomePrice(24.0, max(25.4, exit_qfq), min(22.5, exit_qfq), exit_qfq),
                OutcomePrice(12.0, max(12.7, exit_qfq / 2), min(11.25, exit_qfq / 2), exit_qfq / 2),
                OutcomeTradingStatus.TRADABLE,
                "fixture",
            )
            rows.append(
                PointInTimeSourceRow(
                    feature=feature,
                    anchor_raw_price=12.0,
                    atr20_pct=2.0,
                    outcome_bars=(reference, exit_bar),
                    expected_trade_dates=(exit_date.isoformat(),),
                    settled_at=anchor + timedelta(days=2),
                    source_identity=PointInTimeSourceIdentity(
                        daily_path_hash=HASHES[7],
                        minute_path_hash=HASHES[8],
                        security_fact_hash=HASHES[9],
                        industry_fact_hash=HASHES[10],
                        event_fact_hash=HASHES[11],
                    ),
                    industry_fact=PointInTimeIndustryFact(
                        code=code,
                        industry=f"industry-{index}",
                        classification="official_classification",
                        effective_from=trade_date - timedelta(days=365),
                        effective_to=None,
                        queried_at=anchor + timedelta(days=365),
                        source="official_industry",
                        content_hash=HASHES[10],
                    ),
                    event_facts=(
                        PointInTimeEventFact(
                            fact_id="announcement",
                            published_at=anchor - timedelta(hours=1),
                            effective_at=anchor - timedelta(hours=2),
                            anchor_at=anchor,
                            content_hash=HASHES[12],
                        ),
                    ),
                )
            )
        return PointInTimeDaySource(trade_date, anchor, tuple(rows))


class _MutatingSource(_Source):
    def __init__(self, feature_factory, mutation: str) -> None:
        super().__init__(feature_factory)
        self.mutation = mutation

    def load_day(self, trade_date: date) -> PointInTimeDaySource:
        day = super().load_day(trade_date)
        target = day.rows[1]
        if self.mutation == "dynamic":
            feature = replace(target.feature, quote=replace(target.feature.quote, is_suspended=True))
        else:
            feature = replace(target.feature, history_days=0)
        return replace(day, rows=(day.rows[0], replace(target, feature=feature), day.rows[2]))


class _IncompleteOutcomeSource(_Source):
    def load_day(self, trade_date: date) -> PointInTimeDaySource:
        day = super().load_day(trade_date)
        target = day.rows[0]
        bars = tuple(
            replace(bar, trading_status=OutcomeTradingStatus.UNKNOWN)
            if bar.trade_date == trade_date.isoformat()
            else bar
            for bar in target.outcome_bars
        )
        return replace(day, rows=(replace(target, outcome_bars=bars), *day.rows[1:]))


def test_builder_fails_closed_without_reading_data_when_qualification_is_insufficient() -> None:
    report = PointInTimeDatasetBuilder(_ForbiddenSource()).build(_request(_qualification(ready=False)))

    assert report.state == "historical_data_insufficient"
    assert report.days == ()
    assert report.manifest is None
    assert report.failure_reasons == ("point_in_time_data_not_qualified",)
    assert report.terminal_holdout_opened is False
    assert report.production_authority is False


def test_builder_reconstructs_population_labels_costs_and_keeps_holdout_unread(
    application_feature_factory,
) -> None:
    source = _Source(application_feature_factory)
    request = _request(_qualification())

    report = PointInTimeDatasetBuilder(source).build(request)

    assert report.state == "historical_point_in_time_parity"
    assert source.loaded_dates == list(request.date_split.development_dates)
    assert not set(source.loaded_dates).intersection(request.date_split.terminal_holdout_dates)
    assert len(report.days) == 4
    day = report.days[0]
    by_code = {row.code: row for row in day.rows}
    assert day.benchmark_identity == POINT_IN_TIME_BENCHMARK_ID
    assert day.coverage.total_rows == 3
    assert day.coverage.benchmark_eligible_rows == 2
    assert by_code["600000"].first_rejection_boundary == "eligible"
    assert by_code["600001"].first_rejection_boundary == "board_limit"
    assert by_code["600001"].benchmark_eligible is True
    assert by_code["600002"].first_rejection_boundary == "permanent_eligibility"
    assert tuple(item.cost_bps for item in by_code["600000"].outcomes) == (20, 50, 100)
    assert by_code["600000"].outcomes[0].outcome.benchmark_return_pct == pytest.approx(0.0)
    assert by_code["600000"].outcomes[0].outcome.net_excess_return_pct == pytest.approx(4.8)
    assert by_code["600001"].outcomes[0].outcome.net_excess_return_pct == pytest.approx(-5.2)
    assert report.manifest is not None
    assert report.manifest.date_split_hash == request.date_split.content_hash
    assert report.manifest.terminal_holdout_rows == 0
    assert report.manifest.terminal_holdout_opened is False
    assert report.production_authority is False


def test_candidate_threshold_does_not_change_the_point_in_time_benchmark(
    application_feature_factory,
) -> None:
    source = _Source(application_feature_factory)
    request = _request(_qualification())
    strict_policy = replace(
        request.selection_policy,
        board_policies={
            board: replace(policy, candidate_min_score=100.0)
            for board, policy in request.selection_policy.board_policies.items()
        },
    )

    report = PointInTimeDatasetBuilder(source).build(replace(request, selection_policy=strict_policy))

    by_code = {row.code: row for row in report.days[0].rows}
    assert by_code["600000"].first_rejection_boundary == "candidate_threshold"
    assert by_code["600001"].first_rejection_boundary == "candidate_threshold"
    assert by_code["600000"].benchmark_eligible is True
    assert by_code["600001"].benchmark_eligible is True
    assert by_code["600000"].outcomes[0].outcome.benchmark_return_pct == pytest.approx(0.0)


@pytest.mark.parametrize(
    ("mutation", "expected_boundary", "expected_reason"),
    (
        ("dynamic", "dynamic_hard_filter", "suspended"),
        ("field", "field_eligibility", "strategy_history_insufficient"),
    ),
)
def test_builder_records_dynamic_and_field_first_rejection_boundaries(
    mutation,
    expected_boundary,
    expected_reason,
    application_feature_factory,
) -> None:
    report = PointInTimeDatasetBuilder(_MutatingSource(application_feature_factory, mutation)).build(
        _request(_qualification())
    )

    row = next(item for item in report.days[0].rows if item.code == "600001")
    assert row.first_rejection_boundary == expected_boundary
    assert expected_reason in row.rejection_reasons
    assert row.benchmark_eligible is False


def test_builder_does_not_publish_a_partial_manifest_for_incomplete_benchmark_outcomes(
    application_feature_factory,
) -> None:
    report = PointInTimeDatasetBuilder(_IncompleteOutcomeSource(application_feature_factory)).build(
        _request(_qualification())
    )

    assert report.state == "historical_data_insufficient"
    assert report.days == ()
    assert report.manifest is None
    assert report.failure_reasons == ("benchmark_population_outcome_incomplete",)


def test_artifact_store_round_trips_idempotently_and_rejects_tampering(
    tmp_path,
    application_feature_factory,
) -> None:
    report = PointInTimeDatasetBuilder(_Source(application_feature_factory)).build(_request(_qualification()))
    store = PointInTimeDatasetArtifactStore(tmp_path)

    assert store.write(report).content_hash == report.content_hash
    assert store.write(report).content_hash == report.content_hash
    assert store.verify().content_hash == report.content_hash

    original_request = _request(_qualification())
    conflicting_request = replace(
        original_request,
        selection_policy=replace(original_request.selection_policy, minimum_local_score=1.0),
    )
    assert conflicting_request.selection_policy_hash != original_request.selection_policy_hash
    conflicting = PointInTimeDatasetBuilder(_Source(application_feature_factory)).build(conflicting_request)
    with pytest.raises(PointInTimeDatasetArtifactConflictError, match="identity conflict"):
        store.write(conflicting)

    path = tmp_path / "point-in-time-dataset.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["production_authority"] = True
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(PointInTimeDatasetArtifactConflictError):
        store.verify()
