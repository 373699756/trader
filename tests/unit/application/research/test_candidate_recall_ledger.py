from __future__ import annotations

from collections import Counter
from dataclasses import replace
from datetime import date, datetime, timedelta
from typing import cast
from zoneinfo import ZoneInfo

import pytest

from trader.recommendation.domain.market.feature_contracts import FeatureVector
from trader.recommendation.domain.market.models import Board
from trader.recommendation.domain.publication.models import Strategy
from trader.training.application.candidate_recall_ledger import CandidateRecallLedgerBuilder
from trader.training.application.limited_factor_family import LimitedFactorFamilyResearchBuilder
from trader.training.domain.evaluation.candidate_recall_ledger import (
    CANDIDATE_RECALL_STAGES,
    CandidateRecallDayTrace,
    CandidateRecallDownstreamBoundary,
    CandidateRecallDownstreamTrace,
    CandidateRecallStageLatency,
    CandidateRecallTraceMismatchError,
    attribute_candidate_recall_day,
    build_candidate_recall_report,
)
from trader.training.domain.evaluation.limited_factor_family import (
    LimitedFactorCandidate,
    LimitedFactorFamilySpec,
)
from trader.training.domain.evaluation.models import OutcomeExitStatus, RecommendationOutcome
from trader.training.domain.evaluation.point_in_time_dataset import (
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


def _outcomes(trade_date: date, index: int, net_offset: float = 0.0) -> tuple[PointInTimeCostOutcome, ...]:
    anchor = datetime(trade_date.year, trade_date.month, trade_date.day, 15, 0, tzinfo=SHANGHAI)
    desired_net_20bp = 60.0 - index - net_offset
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


def _day(trade_date: date, population_size: int = 60, net_offset: float = 0.0) -> PointInTimeDayDataset:
    anchor = datetime(trade_date.year, trade_date.month, trade_date.day, 15, 0, tzinfo=SHANGHAI)
    rows = []
    for index in range(population_size):
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
                outcomes=_outcomes(trade_date, index, net_offset),
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


def _dataset(population_sizes: tuple[int, ...] = (60, 60, 60, 60), net_offset: float = 0.0) -> PointInTimeDatasetReport:
    split = _split()
    dates = split.development_dates
    days = tuple(_day(item, size, net_offset) for item, size in zip(dates, population_sizes, strict=True))
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


class _ForbiddenFactorSource:
    def load_candidate_series(self, spec, dataset, recall):  # type: ignore[no-untyped-def]
        raise AssertionError("factor outcomes must not be read for invalid recall evidence")


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
    assert report.rejection_metrics == ()
    assert report.failure_reasons == ("point_in_time_dataset_unavailable",)
    assert report.terminal_holdout_opened is False
    assert report.production_authority is False


def test_first_rejection_losses_are_unique_and_equal_stage_recall_differences() -> None:
    dataset = _dataset()
    report = CandidateRecallLedgerBuilder(_TraceSource(dataset)).build(dataset)

    for day in report.days:
        assert tuple(item.boundary for item in day.rejection_metrics) == CANDIDATE_RECALL_STAGES[1:]
        assert sum(item.rejected_count for item in day.rejection_metrics) == 9
        for index, metric in enumerate(day.rejection_metrics):
            assert metric.rejected_count == metric.positive_net_count == 1
            assert metric.mean_net_excess_return_20bp_pct == 60.0 - index
            for k_index, loss in enumerate(metric.oracle_losses):
                before = day.stages[index].top_k_metrics[k_index]
                after = day.stages[index + 1].top_k_metrics[k_index]
                assert loss.lost_count == before.recalled_count - after.recalled_count == 1
                assert loss.loss_rate == 1 / loss.top_k
    assert sum(item.rejected_count for item in report.rejection_metrics) == 36
    assert all(item.oracle_losses[0].oracle_count == 40 for item in report.rejection_metrics)
    assert all(item.oracle_losses[0].lost_count == 4 for item in report.rejection_metrics)
    assert report.production_authority is False


def test_rejection_results_keep_negative_zero_and_independent_tail_risk_labels() -> None:
    dataset = _dataset(net_offset=55.0)
    report = CandidateRecallLedgerBuilder(_TraceSource(dataset)).build(dataset)
    metrics = {item.boundary: item for item in report.rejection_metrics}

    assert metrics["scoring"].zero_net_count == 4
    assert metrics["scoring"].mean_net_excess_return_20bp_pct == 0.0
    assert metrics["risk"].negative_net_count == 4
    assert metrics["risk"].mean_net_excess_return_20bp_pct == -1.0
    assert metrics["risk"].oracle_losses[0].oracle_count == 40
    assert metrics["risk"].oracle_losses[0].lost_count == 4

    day = _day(dataset.days[0].trade_date)
    trace = _trace(day)
    downstream = tuple(
        replace(row, first_rejection_boundary="risk", rejection_reasons=("risk_rejected",))
        if row.code == "600009"
        else row
        for row in trace.downstream_rows
    )
    attributed = attribute_candidate_recall_day(day, replace(trace, downstream_rows=downstream))
    risk = next(item for item in attributed.rejection_metrics if item.boundary == "risk")
    assert risk.positive_net_count == 2
    assert risk.exit_untradable_rate == risk.severe_loss_rate == 0.5


def test_rejection_micro_denominators_use_each_days_population_and_empty_groups() -> None:
    dataset = _dataset((6, 12, 60, 60))
    report = CandidateRecallLedgerBuilder(_TraceSource(dataset)).build(dataset)
    risk = next(item for item in report.rejection_metrics if item.boundary == "risk")

    assert tuple(item.oracle_count for item in risk.oracle_losses) == (36, 58, 118)
    assert tuple(item.lost_count for item in risk.oracle_losses) == (3, 3, 3)
    assert risk.oracle_losses[0].loss_rate == 3 / 36
    assert (
        risk.oracle_losses[0].loss_rate
        != sum(day.rejection_metrics[6].oracle_losses[0].loss_rate or 0.0 for day in report.days) / 4
    )
    empty = report.days[0].rejection_metrics[6]
    assert empty.rejected_count == 0
    assert empty.mean_net_excess_return_20bp_pct is None
    assert empty.exit_untradable_rate is empty.severe_loss_rate is None
    assert empty.oracle_losses[0].loss_rate == 0.0


@pytest.mark.parametrize("mutation", ("net", "board", "rank", "boundary", "reasons", "risk_labels", "row_hash"))
def test_report_rejects_self_consistent_attribution_with_forged_parent_rows(mutation: str) -> None:
    dataset = _dataset()
    report = CandidateRecallLedgerBuilder(_TraceSource(dataset)).build(dataset)
    parent = dataset.days[0]
    row = parent.rows[0]
    if mutation == "net":
        row = replace(row, outcomes=_outcomes(parent.trade_date, 0, -1.0))
    elif mutation == "board":
        row = replace(row, board=Board.STAR)
    elif mutation == "rank":
        row = replace(row, candidate_rank=1)
    elif mutation == "boundary":
        row = replace(row, first_rejection_boundary="dynamic_hard_filter", rejection_reasons=("dynamic_hard_filter",))
    elif mutation == "reasons":
        row = replace(row, rejection_reasons=("forged_reason",))
    elif mutation == "risk_labels":
        row = replace(
            row,
            outcomes=tuple(replace(item, outcome=replace(item.outcome, severe_drawdown=True)) for item in row.outcomes),
        )
    elif mutation == "row_hash":
        row = replace(row, source_identity=replace(row.source_identity, daily_path_hash=HASHES[12]))
    rows = (row, *parent.rows[1:])
    counts = Counter(item.first_rejection_boundary for item in rows)
    altered_parent = replace(
        parent,
        rows=rows,
        coverage=replace(
            parent.coverage,
            boundary_counts=tuple(
                PointInTimeBoundaryCount(boundary, counts[boundary]) for boundary in POINT_IN_TIME_BOUNDARIES
            ),
        ),
    )
    altered = attribute_candidate_recall_day(altered_parent, _trace(altered_parent))
    # Upstream rows are absent from the downstream trace: the original hash remains valid.
    forged = replace(altered, dataset_day_hash=parent.content_hash, trace_hash=report.days[0].trace_hash)
    if mutation == "row_hash":
        forged = replace(forged, rows=(replace(forged.rows[0], dataset_row_hash=HASHES[12]), *forged.rows[1:]))

    with pytest.raises(CandidateRecallTraceMismatchError, match="parent dataset"):
        build_candidate_recall_report(dataset, (forged, *report.days[1:]))

    dates = tuple(date(2024, 1, day) for day in range(1, 11))
    spec = LimitedFactorFamilySpec(
        family_id="intraday_price_volume_path",
        control_feature_ids=("a", "b", "c", "d", "e", "f"),
        candidates=(
            LimitedFactorCandidate("existing_six_alpha", "control", "unitless", "control"),
            LimitedFactorCandidate("tail_volume_share", "tail_volume_share", "ratio"),
        ),
        selected_candidate_id="tail_volume_share",
        development_dates=dates[:5],
        confirmation_dates=dates[5:],
    )
    # Keep the forged report internally consistent before testing the final consumer.
    # Recompute the aggregates through an altered dataset, then claim the original parent.
    altered_dataset = _dataset()
    altered_days = (altered_parent, *altered_dataset.days[1:])
    assert altered_dataset.manifest is not None
    partitions = tuple(
        replace(partition, day_hashes=(day.content_hash,))
        for partition, day in zip(altered_dataset.manifest.partitions, altered_days, strict=True)
    )
    altered_dataset = replace(
        altered_dataset,
        days=altered_days,
        manifest=replace(altered_dataset.manifest, partitions=partitions),
    )
    source = _TraceSource(altered_dataset)
    altered_report = CandidateRecallLedgerBuilder(source).build(altered_dataset)
    forged_report = replace(
        altered_report,
        dataset_report_hash=dataset.content_hash,
        dataset_manifest_hash=dataset.manifest.content_hash,
    )
    result = LimitedFactorFamilyResearchBuilder(_ForbiddenFactorSource()).build(spec, dataset, forged_report)
    assert result.state == "historical_data_insufficient"
    assert result.failure_reasons == ("candidate_recall_parent_mismatch",)


def test_attribution_rejects_trace_identity_latency_and_metric_tampering() -> None:
    dataset = _dataset()
    report = CandidateRecallLedgerBuilder(_TraceSource(dataset)).build(dataset)
    day = report.days[0]

    with pytest.raises(ValueError, match="trace hash"):
        replace(day, trace_hash=HASHES[12])
    with pytest.raises(ValueError, match="monotonic"):
        replace(day, stages=(replace(day.stages[0], cumulative_latency_ms=99.0), *day.stages[1:]))
    with pytest.raises(ValueError, match="trace hash"):
        replace(
            day,
            stages=tuple(replace(item, cumulative_latency_ms=item.cumulative_latency_ms + 1) for item in day.stages),
        )
    with pytest.raises(ValueError, match="metrics do not match rows"):
        replace(
            day,
            rejection_metrics=(
                replace(day.rejection_metrics[0], mean_net_excess_return_20bp_pct=99.0),
                *day.rejection_metrics[1:],
            ),
        )
    with pytest.raises(ValueError, match="metrics do not match days"):
        replace(report, rejection_metrics=day.rejection_metrics)


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
    assert report.rejection_metrics == ()
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
