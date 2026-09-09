from __future__ import annotations

from collections import Counter
from dataclasses import replace
from datetime import date, datetime, timedelta
from typing import cast
from zoneinfo import ZoneInfo

import pytest

from trader.application.research.candidate_recall_ledger import CandidateRecallLedgerBuilder
from trader.domain.market.feature_contracts import FeatureVector
from trader.domain.market.models import Board
from trader.domain.outcome.models import OutcomeExitStatus, RecommendationOutcome
from trader.domain.recommendation.models import Strategy
from trader.domain.research.candidate_recall_ledger import (
    CANDIDATE_RECALL_STAGES,
    CandidateRecallDayTrace,
    CandidateRecallDownstreamBoundary,
    CandidateRecallDownstreamTrace,
    CandidateRecallStageLatency,
)
from trader.domain.research.point_in_time_dataset import (
    POINT_IN_TIME_BOUNDARIES,
    PointInTimeBoardPopulation,
    PointInTimeBoundaryCount,
    PointInTimeCostOutcome,
    PointInTimeCoverage,
    PointInTimeDatasetManifest,
    PointInTimeDatasetReport,
    PointInTimeDatasetRow,
    PointInTimeDateSplit,
    PointInTimeDayDataset,
    PointInTimeIndustryFact,
    PointInTimePartitionManifest,
    PointInTimeRejectionBoundary,
    PointInTimeSourceIdentity,
)

HASHES = tuple(character * 64 for character in "abcdef0123456789")
SHANGHAI = ZoneInfo("Asia/Shanghai")
FEATURE_HASH = HASHES[0]


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


def _outcomes(trade_date: date, index: int) -> tuple[PointInTimeCostOutcome, ...]:
    anchor = datetime(trade_date.year, trade_date.month, trade_date.day, 14, 50, tzinfo=SHANGHAI)
    desired_net_20bp = 60.0 - index
    gross = desired_net_20bp + 0.2
    severe = index in {9, 10}
    untradable = index == 9
    exit_date = (trade_date + timedelta(days=1)).isoformat()
    return tuple(
        PointInTimeCostOutcome(
            cost_bps,
            RecommendationOutcome(
                snapshot_id=f"point-in-time:{trade_date.isoformat()}:{600000 + index:06d}",
                strategy=Strategy.TOMORROW,
                recommend_date=trade_date.isoformat(),
                stock_code=f"{600000 + index:06d}",
                horizon=1,
                status="complete",
                settled_at=anchor + timedelta(days=2),
                anchor_raw_price=10.0,
                anchor_qfq_price=10.0,
                atr20_pct=2.0,
                minimum_qfq_low=9.6 if severe else 9.9,
                end_qfq_close=10.0 * (1.0 + gross / 100.0),
                exit_status=OutcomeExitStatus.SUSPENDED if untradable else OutcomeExitStatus.TRADABLE,
                untradable_dates=(exit_date,) if untradable else (),
                gross_return_pct=gross,
                benchmark_return_pct=0.0,
                net_excess_return_pct=gross - cost_bps / 100.0,
                mae_pct=-4.0 if severe else -1.0,
                mae_atr=-2.0 if severe else -0.5,
                severe_drawdown=severe,
            ),
        )
        for cost_bps in (20, 50, 100)
    )


def _point_boundary(index: int) -> PointInTimeRejectionBoundary:
    return cast(
        PointInTimeRejectionBoundary,
        {
            0: "permanent_eligibility",
            1: "dynamic_hard_filter",
            2: "field_eligibility",
            3: "candidate_threshold",
            4: "board_limit",
        }.get(index, "eligible"),
    )


def _day(trade_date: date) -> PointInTimeDayDataset:
    anchor = datetime(trade_date.year, trade_date.month, trade_date.day, 14, 50, tzinfo=SHANGHAI)
    rows = []
    for index in range(60):
        code = f"{600000 + index:06d}"
        board = (Board.MAIN, Board.CHINEXT, Board.STAR)[index % 3]
        boundary = _point_boundary(index)
        rows.append(
            PointInTimeDatasetRow(
                code=code,
                trade_date=trade_date,
                anchor_at=anchor,
                board=board,
                industry=f"industry-{index % 4}",
                anchor_raw_price=10.0,
                feature_vector=FeatureVector(FEATURE_HASH, (float(index),), (False,)),
                source_identity=PointInTimeSourceIdentity(
                    daily_path_hash=HASHES[1],
                    minute_path_hash=HASHES[2],
                    security_fact_hash=HASHES[3],
                    industry_fact_hash=HASHES[4],
                    event_fact_hash=HASHES[5],
                ),
                industry_fact=PointInTimeIndustryFact(
                    code=code,
                    industry=f"industry-{index % 4}",
                    classification="official_classification",
                    effective_from=trade_date - timedelta(days=365),
                    effective_to=None,
                    queried_at=anchor + timedelta(days=365),
                    source="official_industry",
                    content_hash=HASHES[4],
                ),
                event_facts=(),
                first_rejection_boundary=boundary,
                rejection_reasons=() if boundary == "eligible" else (boundary,),
                benchmark_eligible=boundary in {"eligible", "candidate_threshold", "board_limit"},
                candidate_eligible=boundary == "eligible",
                candidate_score=None if index < 3 else 100.0 - index,
                candidate_rank=0 if index < 3 else index - 2,
                outcomes=_outcomes(trade_date, index),
            )
        )
    counts = Counter(row.first_rejection_boundary for row in rows)
    coverage = PointInTimeCoverage(
        total_rows=len(rows),
        benchmark_eligible_rows=sum(row.benchmark_eligible for row in rows),
        candidate_eligible_rows=sum(row.candidate_eligible for row in rows),
        label_complete_rows=sum(row.label_complete for row in rows),
        boundary_counts=tuple(
            PointInTimeBoundaryCount(boundary, counts[boundary]) for boundary in POINT_IN_TIME_BOUNDARIES
        ),
    )
    populations = tuple(
        PointInTimeBoardPopulation(
            board,
            f"population-{board.value}",
            sum(row.benchmark_eligible and row.board is board for row in rows),
        )
        for board in (Board.MAIN, Board.CHINEXT, Board.STAR)
    )
    return PointInTimeDayDataset(trade_date, anchor, tuple(rows), coverage, populations)


def _dataset() -> PointInTimeDatasetReport:
    split = _split()
    dates = split.development_dates
    days = tuple(_day(item) for item in dates)
    partitions = tuple(
        PointInTimePartitionManifest(name, (trade_date,), (day.content_hash,), len(day.rows))
        for name, trade_date, day in zip(
            ("training", "early_stopping", "calibration", "confirmation"),
            dates,
            days,
            strict=True,
        )
    )
    manifest = PointInTimeDatasetManifest(
        qualification_hash=HASHES[6],
        daily_archive_manifest_hash=HASHES[7],
        feature_manifest_hash=FEATURE_HASH,
        calendar_hash=HASHES[8],
        security_master_hash=HASHES[9],
        selection_policy_hash=HASHES[10],
        date_split=split,
        date_split_hash=split.content_hash,
        partitions=partitions,
        total_rows=sum(len(day.rows) for day in days),
    )
    return PointInTimeDatasetReport(
        qualification_hash=HASHES[6],
        state="historical_point_in_time_parity",
        days=days,
        manifest=manifest,
        failure_reasons=(),
    )


def _trace(day: PointInTimeDayDataset) -> CandidateRecallDayTrace:
    downstream = []
    for index, row in enumerate(day.rows):
        if not row.candidate_eligible:
            continue
        boundary = {5: "scoring", 6: "risk", 7: "action", 8: "concentration"}.get(index, "selected")
        downstream.append(
            CandidateRecallDownstreamTrace(
                trade_date=day.trade_date,
                code=row.code,
                dataset_row_hash=row.content_hash,
                first_rejection_boundary=cast(CandidateRecallDownstreamBoundary, boundary),
                rejection_reasons=() if boundary == "selected" else (f"{boundary}_rejected",),
            )
        )
    latencies = tuple(
        CandidateRecallStageLatency(stage, float(index)) for index, stage in enumerate(CANDIDATE_RECALL_STAGES)
    )
    return CandidateRecallDayTrace(day.trade_date, day.content_hash, HASHES[11], tuple(downstream), latencies)


class _TraceSource:
    def __init__(self, dataset: PointInTimeDatasetReport) -> None:
        self.traces = {day.trade_date: _trace(day) for day in dataset.days}
        self.loaded: list[date] = []

    def load_day_trace(self, trade_date: date, dataset_day_hash: str) -> CandidateRecallDayTrace | None:
        self.loaded.append(trade_date)
        trace = self.traces.get(trade_date)
        assert trace is None or trace.dataset_day_hash == dataset_day_hash
        return trace


class _ForbiddenTraceSource:
    def load_day_trace(self, trade_date: date, dataset_day_hash: str) -> CandidateRecallDayTrace | None:
        raise AssertionError(f"trace source must not be read: {trade_date}:{dataset_day_hash}")


def _insufficient_dataset() -> PointInTimeDatasetReport:
    return PointInTimeDatasetReport(
        qualification_hash=HASHES[6],
        state="historical_data_insufficient",
        days=(),
        manifest=None,
        failure_reasons=("point_in_time_data_not_qualified",),
    )


def test_ledger_fails_closed_without_reading_trace_when_dataset_is_insufficient() -> None:
    dataset = _insufficient_dataset()

    report = CandidateRecallLedgerBuilder(_ForbiddenTraceSource()).build(dataset)

    assert report.state == "historical_data_insufficient"
    assert report.dataset_report_hash == dataset.content_hash
    assert report.dataset_manifest_hash is None
    assert report.latency_evidence_hash is None
    assert report.days == ()
    assert report.aggregate_stages == ()
    assert report.failure_reasons == ("point_in_time_dataset_unavailable",)
    assert report.terminal_holdout_opened is False
    assert report.production_authority is False


def test_ledger_attributes_all_boundaries_oracle_recall_board_tail_and_latency() -> None:
    dataset = _dataset()
    source = _TraceSource(dataset)

    report = CandidateRecallLedgerBuilder(source).build(dataset)

    assert report.state == "candidate_recall_attributed"
    assert dataset.manifest is not None
    assert source.loaded == list(dataset.manifest.date_split.development_dates)
    assert report.latency_evidence_hash == HASHES[11]
    day = report.days[0]
    rows = {row.code: row for row in day.rows}
    assert rows["600000"].oracle_rank == 1
    assert rows["600000"].first_rejection_boundary == "permanent_eligibility"
    assert rows["600005"].first_rejection_boundary == "scoring"
    assert rows["600006"].first_rejection_boundary == "risk"
    assert rows["600007"].first_rejection_boundary == "action"
    assert rows["600008"].first_rejection_boundary == "concentration"
    assert rows["600009"].first_rejection_boundary == "selected"

    stages = {stage.stage: stage for stage in day.stages}
    assert stages["population"].retained_count == 60
    assert stages["permanent_eligibility"].top_k_metrics[0].recalled_count == 9
    assert stages["candidate_threshold"].top_k_metrics[0].recall == pytest.approx(0.6)
    main = next(
        item for item in stages["candidate_threshold"].top_k_metrics[0].board_recalls if item.board is Board.MAIN
    )
    assert (main.oracle_count, main.recalled_count, main.recall) == (4, 2, 0.5)
    final = stages["concentration"]
    assert final.retained_count == 51
    assert tuple(item.recalled_count for item in final.top_k_metrics) == (1, 11, 41)
    assert final.exit_untradable_rate == pytest.approx(1 / 51)
    assert final.severe_loss_rate == pytest.approx(2 / 51)
    assert final.cumulative_latency_ms == 9.0

    aggregate = {stage.stage: stage for stage in report.aggregate_stages}["concentration"]
    assert aggregate.total_retained_count == 204
    assert aggregate.mean_retained_count == 51.0
    assert aggregate.top_k_metrics[0].recall == pytest.approx(0.1)
    assert aggregate.p50_cumulative_latency_ms == 9.0
    assert aggregate.p95_cumulative_latency_ms == 9.0
    assert report.terminal_holdout_opened is False
    assert report.production_authority is False

    with pytest.raises(ValueError, match="do not match rows"):
        replace(
            day,
            stages=(replace(day.stages[0], retained_count=59), *day.stages[1:]),
        )
    with pytest.raises(ValueError, match="oracle order"):
        replace(
            day,
            rows=(
                replace(day.rows[0], oracle_rank=2),
                replace(day.rows[1], oracle_rank=1),
                *day.rows[2:],
            ),
        )
    with pytest.raises(ValueError, match="do not match days"):
        replace(
            report,
            aggregate_stages=(
                replace(report.aggregate_stages[0], total_retained_count=239, mean_retained_count=59.75),
                *report.aggregate_stages[1:],
            ),
        )


def test_ledger_discards_partial_work_when_a_day_trace_is_missing() -> None:
    dataset = _dataset()
    source = _TraceSource(dataset)
    source.traces.pop(dataset.days[2].trade_date)

    report = CandidateRecallLedgerBuilder(source).build(dataset)

    assert report.state == "historical_data_insufficient"
    assert report.days == ()
    assert report.aggregate_stages == ()
    assert report.failure_reasons == ("candidate_recall_trace_unavailable",)


def test_ledger_fails_closed_when_trace_row_hash_does_not_match() -> None:
    dataset = _dataset()
    source = _TraceSource(dataset)
    trade_date = dataset.days[0].trade_date
    trace = source.traces[trade_date]
    source.traces[trade_date] = replace(
        trace,
        downstream_rows=(
            replace(trace.downstream_rows[0], dataset_row_hash=HASHES[12]),
            *trace.downstream_rows[1:],
        ),
    )

    report = CandidateRecallLedgerBuilder(source).build(dataset)

    assert report.state == "historical_data_insufficient"
    assert report.days == ()
    assert report.failure_reasons == ("candidate_recall_trace_invalid",)


def test_ledger_fails_closed_when_latency_evidence_identity_changes_between_days() -> None:
    dataset = _dataset()
    source = _TraceSource(dataset)
    trade_date = dataset.days[1].trade_date
    source.traces[trade_date] = replace(source.traces[trade_date], latency_evidence_hash=HASHES[12])

    report = CandidateRecallLedgerBuilder(source).build(dataset)

    assert report.state == "historical_data_insufficient"
    assert report.days == ()
    assert report.failure_reasons == ("candidate_recall_trace_invalid",)
