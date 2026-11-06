from __future__ import annotations

from collections import Counter

from dataclasses import replace

from datetime import date



import pytest


from trader.recommendation.domain.market.models import Board


from trader.training.application.candidate_recall_ledger import CandidateRecallLedgerBuilder

from trader.training.application.limited_factor_family import LimitedFactorFamilyResearchBuilder

from trader.training.domain.evaluation.candidate_recall_ledger import CANDIDATE_RECALL_STAGES, AttributeCandidateRecallDay, BuildCandidateRecallReport, CandidateRecallTraceMismatchError

from trader.training.domain.evaluation.limited_factor_family import (
    LimitedFactorCandidate,
    LimitedFactorFamilySpec,
)


from trader.training.domain.evaluation.point_in_time_dataset import POINT_IN_TIME_BOUNDARIES, PointInTimeBoundaryCount

from tests.helpers.training.research.application.candidate_recall_ledger import (
    HASHES,
    _ForbiddenFactorSource,
    _ForbiddenTraceSource,
    _TraceSource,
    _dataset,
    _day,
    _insufficient_dataset,
    _outcomes,
    _trace,
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
    attributed = AttributeCandidateRecallDay(day, replace(trace, downstream_rows=downstream))
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
    altered = AttributeCandidateRecallDay(altered_parent, _trace(altered_parent))
    # Upstream rows are absent from the downstream trace: the original hash remains valid.
    forged = replace(altered, dataset_day_hash=parent.content_hash, trace_hash=report.days[0].trace_hash)
    if mutation == "row_hash":
        forged = replace(forged, rows=(replace(forged.rows[0], dataset_row_hash=HASHES[12]), *forged.rows[1:]))

    with pytest.raises(CandidateRecallTraceMismatchError, match="parent dataset"):
        BuildCandidateRecallReport(dataset, (forged, *report.days[1:]))

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
