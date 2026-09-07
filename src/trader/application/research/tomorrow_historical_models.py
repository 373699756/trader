"""Typed report boundary for the frozen Tomorrow historical historical contract."""

from __future__ import annotations

import dataclasses
import math
import re
from dataclasses import dataclass
from typing import Literal

from trader.application.research.replay_models import canonical_hash
from trader.domain.research.historical_screening import HISTORICAL_SCREENING_SPEC
from trader.domain.research.tomorrow_historical import (
    TOMORROW_HISTORICAL_CANDIDATE_ID,
    TOMORROW_HISTORICAL_SPEC,
)

TomorrowHistoricalStatus = Literal["historical_passed", "historical_rejected"]
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REASON = re.compile(r"^[a-z0-9_]{1,64}$")


@dataclass(frozen=True)
class TomorrowHistoricalGateMetrics:
    archive_coverage: float
    training_trade_dates: int
    validation_trade_dates: int
    validation_pairs: int
    mean_net_increment_20bp: float | None
    mean_net_increment_50bp: float | None
    mean_net_increment_100bp: float | None
    bootstrap_lower_bound_20bp: float | None
    baseline_severe_loss_rate: float | None
    candidate_severe_loss_rate: float | None
    turnover_increase: float | None
    mean_rank_ic: float | None
    top_bottom_quintile_spread: float | None
    maximum_stock_positive_fraction: float | None
    top_five_positive_fraction: float | None
    maximum_board_fraction: float | None

    def __post_init__(self) -> None:
        if not math.isfinite(self.archive_coverage) or not 0.0 <= self.archive_coverage <= 1.0:
            raise ValueError("Tomorrow historical archive coverage must be in [0, 1]")
        if min(self.training_trade_dates, self.validation_trade_dates, self.validation_pairs) < 0:
            raise ValueError("Tomorrow historical historical counts cannot be negative")
        optional = (
            self.mean_net_increment_20bp,
            self.mean_net_increment_50bp,
            self.mean_net_increment_100bp,
            self.bootstrap_lower_bound_20bp,
            self.baseline_severe_loss_rate,
            self.candidate_severe_loss_rate,
            self.turnover_increase,
            self.mean_rank_ic,
            self.top_bottom_quintile_spread,
            self.maximum_stock_positive_fraction,
            self.top_five_positive_fraction,
            self.maximum_board_fraction,
        )
        if any(value is not None and not math.isfinite(value) for value in optional):
            raise ValueError("Tomorrow historical historical metrics must be finite when present")
        rates = (
            self.baseline_severe_loss_rate,
            self.candidate_severe_loss_rate,
            self.maximum_stock_positive_fraction,
            self.top_five_positive_fraction,
            self.maximum_board_fraction,
        )
        if any(value is not None and not 0.0 <= value <= 1.0 for value in rates):
            raise ValueError("Tomorrow historical historical rate metrics must be in [0, 1]")


@dataclass(frozen=True)
class TomorrowHistoricalReport:
    research_spec_hash: str
    source_spec_hash: str
    source_manifest_hash: str
    source_universe_hash: str
    source_histories_hash: str
    candidate_id: str
    status: TomorrowHistoricalStatus
    metrics: TomorrowHistoricalGateMetrics
    training_evidence_hash: str | None
    validation_evidence_hash: str | None
    model_artifact_hash: str | None
    failure_reasons: tuple[str, ...]
    production_authority: bool = False
    schema_version: str = "score_tomorrow_historical_report"
    content_hash: str = dataclasses.field(init=False)

    def __post_init__(self) -> None:
        if (
            self.research_spec_hash != TOMORROW_HISTORICAL_SPEC.content_hash
            or self.source_spec_hash != HISTORICAL_SCREENING_SPEC.content_hash
            or _SHA256.fullmatch(self.source_manifest_hash) is None
            or _SHA256.fullmatch(self.source_universe_hash) is None
            or _SHA256.fullmatch(self.source_histories_hash) is None
        ):
            raise ValueError("Tomorrow historical report source identity is invalid")
        if self.candidate_id != TOMORROW_HISTORICAL_CANDIDATE_ID:
            raise ValueError("Tomorrow historical report requires the fixed candidate")
        if self.status not in {"historical_passed", "historical_rejected"}:
            raise ValueError("Tomorrow historical report status is invalid")
        evidence_hashes = (self.training_evidence_hash, self.validation_evidence_hash, self.model_artifact_hash)
        if any(value is not None and _SHA256.fullmatch(value) is None for value in evidence_hashes):
            raise ValueError("Tomorrow historical report evidence hash is invalid")
        reasons = tuple(sorted(set(self.failure_reasons)))
        if any(_REASON.fullmatch(reason) is None for reason in reasons):
            raise ValueError("Tomorrow historical report requires bounded failure reasons")
        if self.status == "historical_passed":
            if reasons or any(value is None for value in evidence_hashes) or not _passes_historical_gates(self.metrics):
                raise ValueError("Tomorrow historical passing report must satisfy all historical gates")
        elif not reasons:
            raise ValueError("Tomorrow historical rejected report requires bounded failure reasons")
        if self.production_authority or self.schema_version != "score_tomorrow_historical_report":
            raise ValueError("Tomorrow historical historical report cannot authorize production")
        object.__setattr__(self, "failure_reasons", reasons)
        object.__setattr__(self, "content_hash", canonical_hash(self))

    @property
    def forward_preregistration_eligible(self) -> bool:
        return self.status == "historical_passed"


def _passes_historical_gates(metrics: TomorrowHistoricalGateMetrics) -> bool:
    spec = TOMORROW_HISTORICAL_SPEC
    required = (
        metrics.mean_net_increment_20bp,
        metrics.mean_net_increment_50bp,
        metrics.mean_net_increment_100bp,
        metrics.bootstrap_lower_bound_20bp,
        metrics.baseline_severe_loss_rate,
        metrics.candidate_severe_loss_rate,
        metrics.turnover_increase,
        metrics.mean_rank_ic,
        metrics.top_bottom_quintile_spread,
        metrics.maximum_stock_positive_fraction,
        metrics.top_five_positive_fraction,
        metrics.maximum_board_fraction,
    )
    if any(value is None for value in required):
        return False
    return (
        metrics.archive_coverage >= spec.minimum_archive_coverage
        and metrics.training_trade_dates > 0
        and metrics.validation_trade_dates > 0
        and metrics.validation_pairs >= spec.minimum_validation_pairs
        and metrics.mean_net_increment_20bp is not None
        and metrics.mean_net_increment_20bp > 0.0
        and metrics.bootstrap_lower_bound_20bp is not None
        and metrics.bootstrap_lower_bound_20bp > 0.0
        and metrics.baseline_severe_loss_rate is not None
        and metrics.candidate_severe_loss_rate is not None
        and metrics.candidate_severe_loss_rate <= metrics.baseline_severe_loss_rate
        and metrics.turnover_increase is not None
        and metrics.turnover_increase <= spec.maximum_turnover_increase
        and metrics.mean_rank_ic is not None
        and metrics.mean_rank_ic > 0.0
        and metrics.top_bottom_quintile_spread is not None
        and metrics.top_bottom_quintile_spread > 0.0
        and metrics.maximum_stock_positive_fraction is not None
        and metrics.maximum_stock_positive_fraction <= spec.maximum_stock_positive_fraction
        and metrics.top_five_positive_fraction is not None
        and metrics.top_five_positive_fraction <= spec.maximum_top_five_positive_fraction
        and metrics.maximum_board_fraction is not None
        and metrics.maximum_board_fraction <= spec.maximum_board_fraction
    )


__all__ = [
    "TomorrowHistoricalGateMetrics",
    "TomorrowHistoricalReport",
    "TomorrowHistoricalStatus",
]
