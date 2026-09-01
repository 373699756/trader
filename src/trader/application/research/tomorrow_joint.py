"""Alignment and orchestration for the isolated Tomorrow joint-model study."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from typing import Literal

from trader.domain.research.tomorrow_joint import (
    TomorrowJointAlignedRow,
    TomorrowJointCandidateFamily,
    TomorrowJointFamilyConfirmation,
    TomorrowJointFittedModel,
    TomorrowJointPrediction,
    TomorrowJointPredictionSemantics,
    TomorrowJointRowKey,
    TomorrowJointValidationReport,
    confirm_tomorrow_joint_family,
    fit_tomorrow_joint_candidate_family,
    predict_tomorrow_joint,
)

TomorrowJointProfileId = Literal["v1", "v2", "c3"]
_PROFILES: tuple[TomorrowJointProfileId, ...] = ("v1", "v2", "c3")


@dataclass(frozen=True)
class TomorrowJointSourceRow:
    trade_date: date
    code: str
    candidate_order: int
    label_matured_at: date
    predicted_net_excess_20bp: float
    actual_net_excess_20bp: float
    actual_net_excess_50bp: float
    severe_loss: bool

    def __post_init__(self) -> None:
        TomorrowJointRowKey(self.trade_date, self.code)
        values = (
            self.predicted_net_excess_20bp,
            self.actual_net_excess_20bp,
            self.actual_net_excess_50bp,
        )
        if self.candidate_order < 0 or self.label_matured_at <= self.trade_date:
            raise ValueError("Tomorrow joint source row requires an ordered, mature label")
        if any(not math.isfinite(value) for value in values):
            raise ValueError("Tomorrow joint source row values must be finite")

    @property
    def key(self) -> TomorrowJointRowKey:
        return TomorrowJointRowKey(self.trade_date, self.code)


@dataclass(frozen=True)
class TomorrowJointProfileBatch:
    profile_id: TomorrowJointProfileId
    rows: tuple[TomorrowJointSourceRow, ...]
    prediction_semantics: TomorrowJointPredictionSemantics = "pre_base_score_cost_adjusted_net_excess"

    def __post_init__(self) -> None:
        if self.profile_id not in _PROFILES:
            raise ValueError("Tomorrow joint batch profile must be V1, V2, or C3")
        if self.prediction_semantics != "pre_base_score_cost_adjusted_net_excess":
            raise ValueError("Tomorrow joint batch prediction semantics must precede base-score mapping")
        if not self.rows:
            raise ValueError("Tomorrow joint profile batch cannot be empty")
        keys = tuple(row.key for row in self.rows)
        orders = tuple((row.trade_date, row.candidate_order) for row in self.rows)
        if len(set(keys)) != len(keys) or len(set(orders)) != len(orders):
            raise ValueError("Tomorrow joint profile batch keys and candidate order must be unique")


@dataclass(frozen=True)
class TomorrowJointProfileCoverage:
    profile_id: TomorrowJointProfileId
    supplied_rows: int
    missing_from_union: int
    dropped_outside_common: int

    def __post_init__(self) -> None:
        if min(self.supplied_rows, self.missing_from_union, self.dropped_outside_common) < 0:
            raise ValueError("Tomorrow joint coverage counts cannot be negative")


@dataclass(frozen=True)
class TomorrowJointCoverageReport:
    union_rows: int
    common_rows: int
    lost_rows: int
    profiles: tuple[TomorrowJointProfileCoverage, ...]
    production_authority: bool = False

    def __post_init__(self) -> None:
        if self.union_rows < 1 or not 0 < self.common_rows <= self.union_rows:
            raise ValueError("Tomorrow joint coverage requires a non-empty common intersection")
        if self.lost_rows != self.union_rows - self.common_rows:
            raise ValueError("Tomorrow joint coverage loss is inconsistent")
        if tuple(item.profile_id for item in self.profiles) != _PROFILES:
            raise ValueError("Tomorrow joint coverage must report all profiles in fixed order")
        if self.production_authority:
            raise ValueError("Tomorrow joint coverage cannot authorize production")


@dataclass(frozen=True)
class TomorrowJointAlignedDataset:
    rows: tuple[TomorrowJointAlignedRow, ...]
    coverage: TomorrowJointCoverageReport

    def __post_init__(self) -> None:
        if len(self.rows) != self.coverage.common_rows:
            raise ValueError("Tomorrow joint aligned rows must match reported common coverage")


@dataclass(frozen=True)
class TomorrowJointResearchFit:
    candidate_family: TomorrowJointCandidateFamily
    training_coverage: TomorrowJointCoverageReport
    tuning_coverage: TomorrowJointCoverageReport
    production_authority: bool = False

    def __post_init__(self) -> None:
        if self.production_authority:
            raise ValueError("Tomorrow joint fit cannot authorize production")


@dataclass(frozen=True)
class TomorrowJointInference:
    predictions: tuple[TomorrowJointPrediction, ...]
    coverage: TomorrowJointCoverageReport
    production_authority: bool = False

    def __post_init__(self) -> None:
        if len(self.predictions) != self.coverage.common_rows or self.production_authority:
            raise ValueError("Tomorrow joint inference must remain complete research evidence")


def align_tomorrow_joint_batches(
    batches: tuple[TomorrowJointProfileBatch, ...],
) -> TomorrowJointAlignedDataset:
    """Align V1/V2/C3 on the strict common eligible intersection."""

    by_profile = {batch.profile_id: batch for batch in batches}
    if len(batches) != len(_PROFILES) or tuple(sorted(by_profile)) != tuple(sorted(_PROFILES)):
        raise ValueError("Tomorrow joint alignment requires exactly one V1, V2, and C3 batch")
    rows_by_profile = {profile_id: {row.key: row for row in by_profile[profile_id].rows} for profile_id in _PROFILES}
    union = frozenset().union(*(frozenset(rows) for rows in rows_by_profile.values()))
    common = frozenset.intersection(*(frozenset(rows) for rows in rows_by_profile.values()))
    if not common:
        raise ValueError("Tomorrow joint batches have no common eligible intersection")
    aligned: list[TomorrowJointAlignedRow] = []
    for key in sorted(common):
        v1 = rows_by_profile["v1"][key]
        v2 = rows_by_profile["v2"][key]
        c3 = rows_by_profile["c3"][key]
        metadata = {
            (
                row.candidate_order,
                row.label_matured_at,
                row.actual_net_excess_20bp,
                row.actual_net_excess_50bp,
                row.severe_loss,
            )
            for row in (v1, v2, c3)
        }
        if len(metadata) != 1:
            raise ValueError("Tomorrow joint common rows must share metadata and mature labels")
        aligned.append(
            TomorrowJointAlignedRow(
                trade_date=key.trade_date,
                code=key.code,
                candidate_order=v1.candidate_order,
                label_matured_at=v1.label_matured_at,
                actual_net_excess_20bp=v1.actual_net_excess_20bp,
                actual_net_excess_50bp=v1.actual_net_excess_50bp,
                severe_loss=v1.severe_loss,
                v1_predicted_net_excess_20bp=v1.predicted_net_excess_20bp,
                v2_predicted_net_excess_20bp=v2.predicted_net_excess_20bp,
                c3_predicted_net_excess_20bp=c3.predicted_net_excess_20bp,
            )
        )
    ordered = tuple(sorted(aligned, key=lambda item: (item.trade_date, item.candidate_order, item.code)))
    profiles = tuple(
        TomorrowJointProfileCoverage(
            profile_id=profile_id,
            supplied_rows=len(rows_by_profile[profile_id]),
            missing_from_union=len(union - frozenset(rows_by_profile[profile_id])),
            dropped_outside_common=len(frozenset(rows_by_profile[profile_id]) - common),
        )
        for profile_id in _PROFILES
    )
    return TomorrowJointAlignedDataset(
        rows=ordered,
        coverage=TomorrowJointCoverageReport(
            union_rows=len(union),
            common_rows=len(common),
            lost_rows=len(union) - len(common),
            profiles=profiles,
        ),
    )


def fit_tomorrow_joint_research(
    *,
    training_batches: tuple[TomorrowJointProfileBatch, ...],
    tuning_batches: tuple[TomorrowJointProfileBatch, ...],
) -> TomorrowJointResearchFit:
    training = align_tomorrow_joint_batches(training_batches)
    tuning = align_tomorrow_joint_batches(tuning_batches)
    return TomorrowJointResearchFit(
        candidate_family=fit_tomorrow_joint_candidate_family(training.rows, tuning.rows),
        training_coverage=training.coverage,
        tuning_coverage=tuning.coverage,
    )


def produce_tomorrow_joint_predictions(
    model: TomorrowJointFittedModel,
    batches: tuple[TomorrowJointProfileBatch, ...],
) -> TomorrowJointInference:
    aligned = align_tomorrow_joint_batches(batches)
    return TomorrowJointInference(
        predictions=predict_tomorrow_joint(model, aligned.rows),
        coverage=aligned.coverage,
    )


def confirm_tomorrow_joint_research(
    fit: TomorrowJointResearchFit,
    reports: tuple[TomorrowJointValidationReport, ...],
) -> TomorrowJointFamilyConfirmation:
    return confirm_tomorrow_joint_family(fit.candidate_family, reports)


__all__ = [
    "TomorrowJointAlignedDataset",
    "TomorrowJointCoverageReport",
    "TomorrowJointInference",
    "TomorrowJointProfileBatch",
    "TomorrowJointProfileCoverage",
    "TomorrowJointProfileId",
    "TomorrowJointResearchFit",
    "TomorrowJointSourceRow",
    "align_tomorrow_joint_batches",
    "confirm_tomorrow_joint_research",
    "fit_tomorrow_joint_research",
    "produce_tomorrow_joint_predictions",
]
