"""Engineering evidence for a fixed D25 event model on untouched validation dates."""

import math
from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True, slots=True)
class D25ProbabilityAssessment:
    validation_dates: tuple[date, ...]
    scored_dates: tuple[date, ...]
    sample_rows: int
    labeled_rows: int
    scored_rows: int
    positive_rows: int
    training_rows: int
    training_positive_rows: int
    brier: float | None
    baseline_brier: float | None

    def __post_init__(self) -> None:
        if (
            not self.validation_dates or self.validation_dates != tuple(sorted(set(self.validation_dates)))
            or self.scored_dates != tuple(sorted(set(self.scored_dates)))
            or not set(self.scored_dates) <= set(self.validation_dates)
            or not 0 <= self.positive_rows <= self.scored_rows <= self.labeled_rows <= self.sample_rows
            or not 0 < self.training_positive_rows < self.training_rows
            or len(self.scored_dates) > self.scored_rows or bool(self.scored_dates) != bool(self.scored_rows)
            or any(type(n) is not int for n in (
                self.sample_rows, self.labeled_rows, self.scored_rows, self.positive_rows,
                self.training_rows, self.training_positive_rows,
            ))
        ):
            raise ValueError("d25_probability_assessment_counts_invalid")
        for score in (self.brier, self.baseline_brier):
            if (score is None) != (self.scored_rows == 0) or (
                score is not None and (isinstance(score, bool) or not math.isfinite(score) or not 0 <= score <= 1)
            ):
                raise ValueError("d25_probability_assessment_metric_invalid")

    @property
    def unavailable_reason(self) -> str | None:
        if not self.labeled_rows:
            return "validation_labels_unavailable"
        if not self.scored_rows:
            return "validation_model_coverage_missing"
        if self.validation_dates != self.scored_dates:
            return "validation_dates_incomplete"
        return None


@dataclass(frozen=True, slots=True)
class D25ProbabilityReceipt:
    probability_hash: str
    assessment: D25ProbabilityAssessment
