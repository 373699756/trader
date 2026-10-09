from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from typing import cast

import pytest

from tests.unit.application.research.test_candidate_recall_ledger import _dataset, _day, _insufficient_dataset
from trader.recommendation.domain.risk.decision import RiskDecision, UncertaintyAssessment
from trader.recommendation.domain.scoring.alpha import AlphaScore
from trader.recommendation.domain.selection.execution_cost import ExecutionCost, ExecutionCostScenario
from trader.training.application.risk_cost_uncertainty import (
    RiskCostUncertaintyPrerequisite,
    RiskCostUncertaintyResearchBuilder,
)
from trader.training.domain.evaluation.artifact_identity import canonical_artifact_hash
from trader.training.domain.evaluation.historical import ResearchBoard
from trader.training.domain.evaluation.point_in_time_dataset import (
    PointInTimeDatasetReport,
    PointInTimeDatasetRow,
    PointInTimeEventFact,
)
from trader.training.domain.evaluation.risk_cost_population import (
    RiskCostResearchPartition,
    bind_risk_cost_population,
    validate_risk_cost_samples,
)
from trader.training.domain.evaluation.risk_cost_uncertainty import (
    DeepSeekResearchReview,
    RiskCostUncertaintyReport,
    RiskCostUncertaintySample,
)

MODEL_HASH = "d" * 64
MODEL_ID = "industry_ridge_lightgbm"


def _prerequisite(
    dataset: PointInTimeDatasetReport, partition: RiskCostResearchPartition = "calibration"
) -> RiskCostUncertaintyPrerequisite:
    return RiskCostUncertaintyPrerequisite(
        "historical_validated", "c" * 64, MODEL_HASH, True, (), dataset.content_hash, partition, "v3", MODEL_ID
    )


def _sample(row: PointInTimeDatasetRow) -> RiskCostUncertaintySample:
    outcome = row.outcomes[0].outcome
    assert outcome.gross_return_pct is not None and outcome.benchmark_return_pct is not None
    assert outcome.severe_drawdown is not None
    gross_excess = (outcome.gross_return_pct - outcome.benchmark_return_pct) / 100
    return RiskCostUncertaintySample(
        row.trade_date,
        cast(ResearchBoard, row.board.value),
        row.industry,
        AlphaScore(row.code, "v3", MODEL_ID, MODEL_HASH, canonical_artifact_hash(row.feature_vector), 90, 0.01),
        RiskDecision(row.code, 0, False, (), UncertaintyAssessment(None, None, None, None, None, None)),
        ExecutionCost(
            row.code,
            0.002,
            None,
            None,
            tuple(ExecutionCostScenario(f"cost_{bps}bp", bps / 10000) for bps in (20, 50, 100)),
        ),
        DeepSeekResearchReview("failed", None, 0, False, (), "ignored"),
        gross_excess,
        gross_excess - 0.002,
        outcome.severe_drawdown,
        row.content_hash,
        (),
    )


def _samples(
    dataset: PointInTimeDatasetReport, partition: RiskCostResearchPartition = "calibration"
) -> tuple[RiskCostUncertaintySample, ...]:
    assert dataset.manifest is not None
    dates = next(item.dates for item in dataset.manifest.partitions if item.name == partition)
    return tuple(
        _sample(row) for day in dataset.days if day.trade_date in dates for row in day.rows if row.candidate_eligible
    )


def _with_parent_facts(dataset: PointInTimeDatasetReport) -> PointInTimeDatasetReport:
    assert dataset.manifest is not None
    days = tuple(
        replace(
            day,
            rows=tuple(
                replace(
                    row,
                    event_facts=(
                        PointInTimeEventFact(
                            "verified_fact",
                            row.anchor_at - timedelta(days=1),
                            row.anchor_at + timedelta(days=3),
                            row.anchor_at,
                            canonical_artifact_hash((row.trade_date, row.code, "verified_fact")),
                        ),
                    ),
                )
                for row in day.rows
            ),
        )
        for day in dataset.days
    )
    partitions = tuple(
        replace(partition, day_hashes=tuple(day.content_hash for day in days if day.trade_date in partition.dates))
        for partition in dataset.manifest.partitions
    )
    return replace(dataset, days=days, manifest=replace(dataset.manifest, partitions=partitions))


class _Evidence:
    def __init__(self, samples: tuple[RiskCostUncertaintySample, ...] | None) -> None:
        self.samples = samples
        self.calls = 0

    def load_samples(
        self, prerequisite: RiskCostUncertaintyPrerequisite
    ) -> tuple[RiskCostUncertaintySample, ...] | None:
        self.calls += 1
        assert prerequisite.status == "historical_validated"
        return self.samples


def _assert_closed(report: RiskCostUncertaintyReport) -> None:
    assert report.status == "historical_data_insufficient"
    assert report.calibration is None
    assert report.ablation == report.decisions == ()
    assert report.evidence_hash is report.research_identity is None
    assert report.production_authority is report.terminal_holdout_opened is False


def test_parent_historical_insufficiency_returns_typed_report_without_reading_evidence() -> None:
    dataset = _insufficient_dataset()
    evidence = _Evidence(None)
    prerequisite = replace(
        _prerequisite(dataset),
        status="historical_data_insufficient",
        model_artifact_hash=None,
        point_in_time_parity=False,
        failure_reasons=("daily_close_proxy_not_point_in_time",),
    )
    report = RiskCostUncertaintyResearchBuilder(evidence).build(prerequisite, dataset)
    _assert_closed(report)
    assert evidence.calls == 0
    assert report.failure_reasons == ("daily_close_proxy_not_point_in_time", "risk_cost_parent_evidence_unavailable")


@pytest.mark.parametrize("kind", ("insufficient", "wrong_hash"))
def test_population_failure_is_detected_before_reading_samples(kind: str) -> None:
    dataset = _dataset() if kind == "wrong_hash" else _insufficient_dataset()
    prerequisite = _prerequisite(dataset)
    if kind == "wrong_hash":
        prerequisite = replace(prerequisite, dataset_report_hash="a" * 64)
    evidence = _Evidence(None)
    report = RiskCostUncertaintyResearchBuilder(evidence).build(prerequisite, dataset)
    _assert_closed(report)
    assert evidence.calls == 0
    assert report.failure_reasons == ("risk_cost_population_unavailable",)


@pytest.mark.parametrize("partition", ("calibration", "confirmation"))
def test_complete_partition_binds_exact_population_outcomes_features_and_model(
    partition: RiskCostResearchPartition,
) -> None:
    dataset = _dataset()
    source = _Evidence(tuple(reversed(_samples(dataset, partition))))
    report = RiskCostUncertaintyResearchBuilder(source).build(_prerequisite(dataset, partition), dataset)
    assert source.calls == 1
    assert report.status == "evaluated"
    assert report.sample_count == 55
    assert report.research_identity == bind_risk_cost_population(dataset, partition, "v3", MODEL_ID, MODEL_HASH)
    assert report.production_authority is report.terminal_holdout_opened is False
    assert report.research_identity is not None
    assert {item.trade_date for item in report.decisions} == set(report.research_identity.dates)
    assert all(item.evaluated_count == 55 for item in report.ablation)


@pytest.mark.parametrize(
    "mutation",
    (
        "empty",
        "missing",
        "duplicate",
        "training",
        "early_stopping",
        "confirmation",
        "holdout",
        "row_hash",
        "board",
        "industry",
        "feature",
        "model",
        "profile",
        "model_id",
        "gross",
        "severe",
        "missing_cost",
        "wrong_cost",
    ),
)
def test_invalid_sample_binding_discards_entire_report(mutation: str) -> None:
    dataset = _dataset()
    samples = _samples(dataset)
    first = samples[0]
    if mutation == "empty":
        samples = ()
    elif mutation == "missing":
        samples = samples[1:]
    elif mutation == "duplicate":
        samples = (*samples, first)
    elif mutation in {"training", "early_stopping", "confirmation", "holdout"}:
        assert dataset.manifest is not None
        dates = {
            "training": dataset.manifest.date_split.training_dates,
            "early_stopping": dataset.manifest.date_split.early_stopping_dates,
            "confirmation": dataset.manifest.date_split.confirmation_dates,
            "holdout": dataset.manifest.date_split.terminal_holdout_dates,
        }[mutation]
        samples = (replace(first, trade_date=dates[0]), *samples[1:])
    else:
        if mutation == "row_hash":
            first = replace(first, dataset_row_hash="a" * 64)
        elif mutation == "board":
            first = replace(first, board="main")
        elif mutation == "industry":
            first = replace(first, industry="forged_industry")
        elif mutation == "feature":
            first = replace(first, alpha=replace(first.alpha, feature_vector_hash="a" * 64))
        elif mutation == "model":
            first = replace(first, alpha=replace(first.alpha, model_hash="a" * 64))
        elif mutation == "profile":
            first = replace(first, alpha=replace(first.alpha, profile_id="v2"))
        elif mutation == "model_id":
            first = replace(first, alpha=replace(first.alpha, model_id="another_head"))
        elif mutation == "gross":
            first = replace(
                first,
                actual_alpha_return=first.actual_alpha_return + 0.01,
                actual_net_excess_return=first.actual_net_excess_return + 0.01,
            )
        elif mutation == "severe":
            first = replace(first, actual_severe_loss=not first.actual_severe_loss)
        elif mutation == "missing_cost":
            first = replace(first, cost=replace(first.cost, scenarios=first.cost.scenarios[:1]))
        elif mutation == "wrong_cost":
            first = replace(
                first,
                cost=replace(
                    first.cost,
                    scenarios=tuple(
                        replace(item, round_trip_return=0.006) if item.scenario_id == "cost_50bp" else item
                        for item in first.cost.scenarios
                    ),
                ),
            )
        samples = (first, *samples[1:])
    source = _Evidence(samples)
    report = RiskCostUncertaintyResearchBuilder(source).build(_prerequisite(dataset), dataset)
    _assert_closed(report)
    assert source.calls == 1
    assert report.failure_reasons == ("risk_cost_evidence_invalid",)


def test_unavailable_evidence_after_population_validation_retains_no_results() -> None:
    dataset = _dataset()
    source = _Evidence(None)
    report = RiskCostUncertaintyResearchBuilder(source).build(_prerequisite(dataset), dataset)
    _assert_closed(report)
    assert source.calls == 1
    assert report.failure_reasons == ("risk_cost_evidence_unavailable",)


def test_direct_population_validation_rejects_forged_dataset_identity() -> None:
    dataset = _dataset()
    identity = bind_risk_cost_population(dataset, "calibration", "v3", MODEL_ID, MODEL_HASH)
    with pytest.raises(ValueError, match="dataset identity"):
        validate_risk_cost_samples(dataset, replace(identity, partition_hash="a" * 64), _samples(dataset))


def test_prerequisite_refuses_training_and_terminal_holdout() -> None:
    dataset = _dataset()
    for partition in ("training", "terminal_holdout"):
        with pytest.raises(ValueError, match="partition/profile"):
            replace(_prerequisite(dataset), research_partition=partition)


def _two_calibration_days() -> PointInTimeDatasetReport:
    dataset = _dataset()
    assert dataset.manifest is not None
    split = dataset.manifest.date_split
    start = split.training_dates[0]
    split = replace(
        split,
        calibration_dates=(start + timedelta(days=2), start + timedelta(days=3)),
        development_confirmation_embargo_dates=tuple(start + timedelta(days=4 + index) for index in range(5)),
        confirmation_dates=(start + timedelta(days=9),),
        confirmation_holdout_embargo_dates=tuple(start + timedelta(days=10 + index) for index in range(5)),
        terminal_holdout_dates=tuple(start + timedelta(days=15 + index) for index in range(200)),
    )
    days = tuple(_day(item) for item in split.development_dates)
    dates = (split.training_dates, split.early_stopping_dates, split.calibration_dates, split.confirmation_dates)
    partitions = tuple(
        replace(
            partition,
            dates=group,
            day_hashes=tuple(day.content_hash for day in days if day.trade_date in group),
            row_count=sum(len(day.rows) for day in days if day.trade_date in group),
        )
        for partition, group in zip(dataset.manifest.partitions, dates, strict=True)
    )
    manifest = replace(
        dataset.manifest,
        date_split=split,
        date_split_hash=split.content_hash,
        partitions=partitions,
        total_rows=sum(len(day.rows) for day in days),
    )
    return replace(dataset, days=days, manifest=manifest)


def test_entire_missing_day_fails_closed_and_complete_dates_remain_in_denominator() -> None:
    dataset = _with_parent_facts(_two_calibration_days())
    samples = _samples(dataset)
    parent_facts = {(row.trade_date, row.code): row.event_facts for day in dataset.days for row in day.rows}
    first_date = samples[0].trade_date
    incomplete = _Evidence(tuple(item for item in samples if item.trade_date == first_date))
    report = RiskCostUncertaintyResearchBuilder(incomplete).build(_prerequisite(dataset), dataset)
    _assert_closed(report)
    assert incomplete.calls == 1

    source = _Evidence(
        tuple(
            replace(
                item,
                risk=replace(item.risk, veto=True, structured_fact_ids=("verified_fact",)),
                structured_facts=parent_facts[(item.trade_date, item.code)],
            )
            if item.trade_date == first_date
            else item
            for item in samples
        )
    )
    complete = RiskCostUncertaintyResearchBuilder(source).build(_prerequisite(dataset), dataset)
    assert complete.status == "evaluated"
    assert complete.sample_count == 110
    assert all(
        len(cost.days) == 2 and cost.empty_day_count == 1 for arm in complete.ablation for cost in arm.cost_sensitivity
    )


def test_sample_estimated_cost_can_differ_from_fixed_pressure_without_double_deduction() -> None:
    dataset = _dataset()
    samples = tuple(
        replace(
            item,
            cost=replace(item.cost, estimated_round_trip_return=0.005),
            actual_net_excess_return=item.actual_alpha_return - 0.005,
        )
        for item in _samples(dataset)
    )
    report = RiskCostUncertaintyResearchBuilder(_Evidence(samples)).build(_prerequisite(dataset), dataset)
    assert report.status == "evaluated"
    local = report.ablation[0]
    cost20 = local.cost_sensitivity[0]
    assert local.mean_selected_net_excess_return is not None
    assert cost20.mean_daily_slot_net_excess_return == pytest.approx(local.mean_selected_net_excess_return + 0.003)


def test_rejected_parent_preserves_rejection_without_source_reads() -> None:
    dataset = _dataset()
    prerequisite = replace(
        _prerequisite(dataset),
        status="historical_rejected",
        point_in_time_parity=False,
        failure_reasons=("historical_gate_rejected",),
    )
    source = _Evidence(None)
    report = RiskCostUncertaintyResearchBuilder(source).build(prerequisite, dataset)
    assert report.status == "historical_rejected"
    assert source.calls == 0
    assert report.research_identity is None
    assert report.ablation == ()


def test_no_candidate_parent_date_fails_before_evidence_instead_of_fabricating_cash() -> None:
    dataset = _dataset()
    assert dataset.manifest is not None
    day = dataset.days[2]
    rows = tuple(
        replace(
            row, first_rejection_boundary="board_limit", rejection_reasons=("board_limit",), candidate_eligible=False
        )
        if row.candidate_eligible
        else row
        for row in day.rows
    )
    coverage = replace(
        day.coverage,
        candidate_eligible_rows=0,
        boundary_counts=tuple(
            replace(item, count=sum(row.first_rejection_boundary == item.boundary for row in rows))
            for item in day.coverage.boundary_counts
        ),
    )
    day = replace(day, rows=rows, coverage=coverage)
    days = (*dataset.days[:2], day, *dataset.days[3:])
    partitions = tuple(
        replace(item, day_hashes=(day.content_hash,)) if item.name == "calibration" else item
        for item in dataset.manifest.partitions
    )
    dataset = replace(dataset, days=days, manifest=replace(dataset.manifest, partitions=partitions))
    source = _Evidence(None)
    report = RiskCostUncertaintyResearchBuilder(source).build(_prerequisite(dataset), dataset)
    _assert_closed(report)
    assert source.calls == 0
    assert report.failure_reasons == ("risk_cost_population_unavailable",)


def test_adding_rejected_parent_row_fails_exact_candidate_coverage() -> None:
    dataset = _dataset()
    source = _Evidence((*_samples(dataset), _sample(dataset.days[2].rows[0])))
    report = RiskCostUncertaintyResearchBuilder(source).build(_prerequisite(dataset), dataset)
    _assert_closed(report)
    assert report.failure_reasons == ("risk_cost_evidence_invalid",)


def test_sample_cost_inconsistency_cannot_be_constructed() -> None:
    row = _samples(_dataset())[0]
    with pytest.raises(ValueError, match="exactly once"):
        replace(row, actual_net_excess_return=row.actual_net_excess_return - 0.002)


@pytest.mark.parametrize("mutation", ("missing_severe", "cost_label_conflict", "cost_gross_conflict"))
def test_incomplete_or_conflicting_parent_outcomes_fail_before_source_reads(mutation: str) -> None:
    dataset = _dataset()
    assert dataset.manifest is not None
    day = dataset.days[2]
    row = day.rows[5]
    cost = row.outcomes[1]
    outcome = cost.outcome
    if mutation == "missing_severe":
        outcome = replace(outcome, severe_drawdown=None)
    elif mutation == "cost_label_conflict":
        outcome = replace(outcome, severe_drawdown=not outcome.severe_drawdown)
    else:
        assert outcome.gross_return_pct is not None and outcome.net_excess_return_pct is not None
        outcome = replace(
            outcome,
            gross_return_pct=outcome.gross_return_pct + 1,
            net_excess_return_pct=outcome.net_excess_return_pct + 1,
        )
    row = replace(row, outcomes=(row.outcomes[0], replace(cost, outcome=outcome), row.outcomes[2]))
    day = replace(day, rows=(*day.rows[:5], row, *day.rows[6:]))
    partitions = tuple(
        replace(item, day_hashes=(day.content_hash,)) if item.name == "calibration" else item
        for item in dataset.manifest.partitions
    )
    dataset = replace(
        dataset,
        days=(*dataset.days[:2], day, *dataset.days[3:]),
        manifest=replace(dataset.manifest, partitions=partitions),
    )
    source = _Evidence(None)
    report = RiskCostUncertaintyResearchBuilder(source).build(_prerequisite(dataset), dataset)
    _assert_closed(report)
    assert source.calls == 0
    assert report.failure_reasons == ("risk_cost_population_unavailable",)


@pytest.mark.parametrize("owner", ("local", "deepseek", "both"))
def test_bound_visible_fact_allows_risk_effects_without_double_deduction(owner: str) -> None:
    dataset = _with_parent_facts(_dataset())
    samples = _samples(dataset)
    first = samples[0]
    row = next(row for day in dataset.days for row in day.rows if row.content_hash == first.dataset_row_hash)
    fact = row.event_facts[0]
    # An announced future unlock is visible now; effective_at is not the publication deadline.
    assert fact.published_at < row.anchor_at < fact.effective_at
    first = replace(
        first,
        risk=replace(first.risk, penalty_points=10, structured_fact_ids=(fact.fact_id,))
        if owner in {"local", "both"}
        else first.risk,
        deepseek=DeepSeekResearchReview("applied", 95, 3, True, (fact.fact_id,), "ignored")
        if owner in {"deepseek", "both"}
        else first.deepseek,
        structured_facts=(fact,),
    )
    source = _Evidence((first, *samples[1:]))
    report = RiskCostUncertaintyResearchBuilder(source).build(_prerequisite(dataset), dataset)
    assert report.status == "evaluated"
    assert source.calls == 1
    decisions = {item.arm: item for item in report.decisions if item.code == first.code}
    assert decisions["local_only"].score == (80 if owner in {"local", "both"} else 90)
    if owner in {"deepseek", "both"}:
        assert decisions["structured_facts_veto"].selected_rank is None
        assert decisions["fixed_68_32"].selected_rank is None
    assert report.production_authority is report.terminal_holdout_opened is False


@pytest.mark.parametrize("owner", ("local", "deepseek"))
@pytest.mark.parametrize(
    "mutation", ("missing_parent", "hash", "publication", "effective", "other_row", "other_date", "ambiguous")
)
def test_forged_fact_binding_discards_all_ablation_results(mutation: str, owner: str) -> None:
    dataset = _with_parent_facts(_dataset())
    assert dataset.manifest is not None
    day = dataset.days[2]
    row = day.rows[5]
    fact = row.event_facts[0]
    if mutation in {"missing_parent", "ambiguous"}:
        facts = () if mutation == "missing_parent" else (fact, replace(fact, content_hash="a" * 64))
        row = replace(row, event_facts=facts)
        day = replace(day, rows=(*day.rows[:5], row, *day.rows[6:]))
        dataset = replace(
            dataset,
            days=(*dataset.days[:2], day, *dataset.days[3:]),
            manifest=replace(
                dataset.manifest,
                partitions=tuple(
                    replace(item, day_hashes=(day.content_hash,)) if item.name == "calibration" else item
                    for item in dataset.manifest.partitions
                ),
            ),
        )
    elif mutation == "hash":
        fact = replace(fact, content_hash="a" * 64)
    elif mutation == "publication":
        fact = replace(fact, published_at=fact.published_at - timedelta(hours=1))
    elif mutation == "effective":
        fact = replace(fact, effective_at=fact.effective_at + timedelta(days=1))
    elif mutation == "other_row":
        fact = day.rows[6].event_facts[0]
    else:
        fact = dataset.days[3].rows[5].event_facts[0]
    samples = _samples(dataset)
    first = next(item for item in samples if item.dataset_row_hash == row.content_hash)
    forged = replace(
        first,
        risk=replace(first.risk, veto=True, structured_fact_ids=(fact.fact_id,)) if owner == "local" else first.risk,
        deepseek=DeepSeekResearchReview("applied", 95, 3, True, (fact.fact_id,), "ignored")
        if owner == "deepseek"
        else first.deepseek,
        structured_facts=(fact,),
    )
    source = _Evidence(tuple(forged if item.code == first.code else item for item in samples))
    report = RiskCostUncertaintyResearchBuilder(source).build(_prerequisite(dataset), dataset)
    _assert_closed(report)
    assert source.calls == 1
    assert report.failure_reasons == ("risk_cost_evidence_invalid",)


def test_unpublished_future_fact_cannot_be_used_as_risk_evidence() -> None:
    row = _with_parent_facts(_dataset()).days[2].rows[5]
    with pytest.raises(ValueError, match="not visible"):
        replace(row.event_facts[0], published_at=row.anchor_at + timedelta(seconds=1))


def test_invalid_fact_decoding_at_evidence_port_also_returns_closed_report() -> None:
    dataset = _dataset()
    sample = _samples(dataset)[0]

    class _InvalidFacts:
        def load_samples(
            self, prerequisite: RiskCostUncertaintyPrerequisite
        ) -> tuple[RiskCostUncertaintySample, ...] | None:
            return (replace(sample, risk=replace(sample.risk, veto=True, structured_fact_ids=("missing_fact",))),)

    report = RiskCostUncertaintyResearchBuilder(_InvalidFacts()).build(_prerequisite(dataset), dataset)
    _assert_closed(report)
    assert report.failure_reasons == ("risk_cost_evidence_invalid",)
