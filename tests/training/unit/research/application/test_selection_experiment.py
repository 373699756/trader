from __future__ import annotations

from datetime import date, timedelta

from trader.training.application.selection_experiment import (
    PairedSelectionExperimentBuilder,
    SelectionExperimentParentRowIdentity,
    SelectionExperimentPrerequisite,
)
from trader.training.domain.evaluation.point_in_time_dataset import PointInTimeDateSplit
from trader.training.domain.evaluation.selection_experiment import (
    PairedSelectionRow,
    PairedSelectionSpecification,
    SelectionTargetEvidence,
)


def _specification() -> PairedSelectionSpecification:
    return PairedSelectionSpecification(
        strategy="tomorrow",
        dataset_hash="a" * 64,
        dataset_manifest_hash="f" * 64,
        candidate_recall_hash="b" * 64,
        industry_model_id="industry_ridge",
        industry_model_hash="c" * 64,
        pooled_ridge_model_id="pooled_ridge",
        pooled_ridge_model_hash="d" * 64,
        pooled_lightgbm_model_id="pooled_lightgbm",
        pooled_lightgbm_model_hash="e" * 64,
        research_dates=(date(2026, 1, 5),),
        predefined_exit_identity="not_used_for_tomorrow",
        profile_id="v2",
        partition="confirmation",
        configuration_hash="2" * 64,
        cost_reference_hash="3" * 64,
        bootstrap_master_seed=20261022,
        date_split=PointInTimeDateSplit(
            (date(2025, 1, 2),),
            (date(2025, 2, 3),),
            (date(2025, 3, 3),),
            tuple(date(2025, 4, 1) + timedelta(days=index) for index in range(5)),
            (date(2026, 1, 5),),
            tuple(date(2026, 1, 6) + timedelta(days=index) for index in range(5)),
            tuple(date(2026, 1, 12) + timedelta(days=index) for index in range(200)),
        ),
    )


class _Evidence:
    def __init__(self) -> None:
        self.calls = 0

    def load_rows(self, specification, prerequisite):
        del specification, prerequisite
        self.calls += 1
        return None


class _RowsEvidence:
    def __init__(self, rows: tuple[PairedSelectionRow, ...]) -> None:
        self.rows = rows

    def load_rows(self, specification, prerequisite):
        del specification, prerequisite
        return self.rows


def _row(parent_row_hash: str = "1" * 64) -> PairedSelectionRow:
    return PairedSelectionRow(
        trade_date=date(2026, 1, 5),
        code="600001",
        board="main",
        industry="bank",
        parent_row_hash=parent_row_hash,
        rule_score=80.0,
        estimated_round_trip_cost=0.002,
        risk_eligible=True,
        action_eligible=True,
        capacity_sufficient=True,
        execution_capacity=1_000_000.0,
        severe_loss=False,
        mae_atr20=-0.2,
        target_evidence=(SelectionTargetEvidence("t1", 0.01, 0.011, 0.012, 0.013, False),),
        point_in_time_parity=True,
    )


def _ready_prerequisite(parent_row_hash: str = "1" * 64) -> SelectionExperimentPrerequisite:
    return SelectionExperimentPrerequisite(
        specification_hash=_specification().content_hash,
        qualification_hash="4" * 64,
        dataset_status="historical_point_in_time_parity",
        candidate_recall_status="candidate_recall_attributed",
        dataset_hash="a" * 64,
        dataset_manifest_hash="f" * 64,
        candidate_recall_hash="b" * 64,
        point_in_time_parity=True,
        terminal_holdout_opened=False,
        population_rows=(SelectionExperimentParentRowIdentity(date(2026, 1, 5), "600001", parent_row_hash),),
        failure_reasons=(),
    )


def test_selection_experiment_does_not_read_returns_before_point_in_time_and_recall_prerequisites() -> None:
    evidence = _Evidence()
    report = PairedSelectionExperimentBuilder(evidence).build(
        _specification(),
        SelectionExperimentPrerequisite(
            specification_hash=_specification().content_hash,
            qualification_hash="4" * 64,
            dataset_status="historical_data_insufficient",
            candidate_recall_status="historical_data_insufficient",
            dataset_hash="a" * 64,
            dataset_manifest_hash="f" * 64,
            candidate_recall_hash="b" * 64,
            point_in_time_parity=False,
            terminal_holdout_opened=False,
            population_rows=(),
            failure_reasons=("point_in_time_data_not_qualified",),
        ),
    )

    assert report.status == "historical_data_insufficient"
    assert report.results == ()
    assert evidence.calls == 0
    assert "selection_experiment_prerequisite_unavailable" in report.failure_reasons


def test_selection_experiment_requires_exact_parent_row_coverage_before_evaluation() -> None:
    completed = PairedSelectionExperimentBuilder(_RowsEvidence((_row(),))).build(
        _specification(),
        _ready_prerequisite(),
    )
    mismatched = PairedSelectionExperimentBuilder(_RowsEvidence((_row("2" * 64),))).build(
        _specification(),
        _ready_prerequisite(),
    )

    assert completed.status == "historical_experiment_completed"
    assert mismatched.status == "historical_data_insufficient"
    assert mismatched.failure_reasons == ("selection_experiment_evidence_invalid",)
