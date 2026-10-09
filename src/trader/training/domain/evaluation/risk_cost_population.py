"""Point-in-time population binding for isolated Tomorrow risk-cost studies."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Literal

from trader.recommendation.domain.scoring.profile_identity import ScoringProfileId
from trader.training.domain.evaluation.artifact_identity import canonical_artifact_hash
from trader.training.domain.evaluation.point_in_time_dataset import PointInTimeDatasetReport, PointInTimeDatasetRow

if TYPE_CHECKING:
    from trader.training.domain.evaluation.risk_cost_uncertainty import RiskCostUncertaintySample

RiskCostResearchPartition = Literal["calibration", "confirmation"]
_HASH = re.compile(r"^[0-9a-f]{64}$")
_ID = re.compile(r"^[a-z0-9_]{1,96}$")


@dataclass(frozen=True)
class RiskCostResearchIdentity:
    dataset_report_hash: str
    dataset_manifest_hash: str
    partition_hash: str
    partition: RiskCostResearchPartition
    dates: tuple[date, ...]
    profile_id: ScoringProfileId
    model_id: str
    model_artifact_hash: str

    def __post_init__(self) -> None:
        hashes = (self.dataset_report_hash, self.dataset_manifest_hash, self.partition_hash, self.model_artifact_hash)
        if any(_HASH.fullmatch(item) is None for item in hashes):
            raise ValueError("risk-cost research identity hashes are invalid")
        if self.partition not in {"calibration", "confirmation"} or self.profile_id not in {"v2", "v3"}:
            raise ValueError("risk-cost research partition/profile is invalid")
        dates = tuple(self.dates)
        if not dates or dates != tuple(sorted(set(dates))) or _ID.fullmatch(self.model_id) is None:
            raise ValueError("risk-cost research dates/model identity are invalid")
        object.__setattr__(self, "dates", dates)


def bind_risk_cost_population(
    dataset: PointInTimeDatasetReport,
    partition: RiskCostResearchPartition,
    profile_id: ScoringProfileId,
    model_id: str,
    model_artifact_hash: str,
) -> RiskCostResearchIdentity:
    if dataset.state != "historical_point_in_time_parity" or dataset.manifest is None:
        raise ValueError("risk-cost point-in-time dataset is unavailable")
    if partition not in {"calibration", "confirmation"}:
        raise ValueError("risk-cost cannot read training, embargo or terminal holdout")
    selected = next(item for item in dataset.manifest.partitions if item.name == partition)
    days = tuple(day for day in dataset.days if day.trade_date in selected.dates)
    if any(not any(row.candidate_eligible for row in day.rows) for day in days):
        raise ValueError("risk-cost empty candidate population cannot be inferred as cash")
    for day in days:
        for row in day.rows:
            if row.candidate_eligible:
                _parent_outcome(row)
    return RiskCostResearchIdentity(
        dataset.content_hash,
        dataset.manifest.content_hash,
        selected.content_hash,
        partition,
        selected.dates,
        profile_id,
        model_id,
        model_artifact_hash,
    )


def validate_risk_cost_samples(
    dataset: PointInTimeDatasetReport,
    identity: RiskCostResearchIdentity,
    samples: tuple[RiskCostUncertaintySample, ...],
) -> None:
    expected_identity = bind_risk_cost_population(
        dataset, identity.partition, identity.profile_id, identity.model_id, identity.model_artifact_hash
    )
    if identity != expected_identity:
        raise ValueError("risk-cost dataset identity does not match parent")
    parents = {
        (row.trade_date, row.code): row
        for day in dataset.days
        if day.trade_date in identity.dates
        for row in day.rows
        if row.candidate_eligible
    }
    keys = tuple((sample.trade_date, sample.code) for sample in samples)
    if len(keys) != len(set(keys)) or set(keys) != set(parents):
        raise ValueError("risk-cost samples must cover the exact candidate population")
    for sample in samples:
        _validate_parent_row(parents[(sample.trade_date, sample.code)], sample, identity)


def _validate_parent_row(
    row: PointInTimeDatasetRow, sample: RiskCostUncertaintySample, identity: RiskCostResearchIdentity
) -> None:
    if (
        sample.dataset_row_hash != row.content_hash
        or sample.board != row.board.value
        or sample.industry != row.industry
        or sample.alpha.feature_vector_hash != canonical_artifact_hash(row.feature_vector)
        or sample.alpha.model_hash != identity.model_artifact_hash
        or sample.alpha.model_id != identity.model_id
        or sample.alpha.profile_id != identity.profile_id
    ):
        raise ValueError("risk-cost sample input identity does not match parent")
    _validate_structured_facts(row, sample)
    gross_excess, severe = _parent_outcome(row)
    if (
        not math.isclose(sample.actual_alpha_return, gross_excess, rel_tol=0.0, abs_tol=1e-12)
        or sample.actual_severe_loss is not severe
    ):
        raise ValueError("risk-cost sample outcome does not match parent")


def _validate_structured_facts(row: PointInTimeDatasetRow, sample: RiskCostUncertaintySample) -> None:
    for fact in sample.structured_facts:
        matches = tuple(item for item in row.event_facts if item.fact_id == fact.fact_id)
        if len(matches) != 1 or matches[0] != fact:
            raise ValueError("risk-cost structured fact must match one unambiguous parent event")


def _parent_outcome(row: PointInTimeDatasetRow) -> tuple[float, bool]:
    values = []
    for item in row.outcomes:
        outcome = item.outcome
        if (
            outcome.status != "complete"
            or outcome.gross_return_pct is None
            or outcome.benchmark_return_pct is None
            or outcome.severe_drawdown is None
            or outcome.exit_untradable is None
        ):
            raise ValueError("risk-cost parent outcome is incomplete")
        gross_excess = (outcome.gross_return_pct - outcome.benchmark_return_pct) / 100.0
        values.append((gross_excess, outcome.severe_drawdown, outcome.exit_untradable))
    gross_excess, severe, exit_untradable = values[0]
    if any(
        not math.isclose(value, gross_excess, rel_tol=0.0, abs_tol=1e-12)
        or item_severe is not severe
        or item_exit is not exit_untradable
        for value, item_severe, item_exit in values
    ):
        raise ValueError("risk-cost parent cost outcomes must share gross excess and risk labels")
    return gross_excess, severe


__all__ = [
    "RiskCostResearchIdentity",
    "RiskCostResearchPartition",
    "bind_risk_cost_population",
    "validate_risk_cost_samples",
]
