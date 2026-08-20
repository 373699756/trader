"""Immutable evidence and reports for Score-R6 research."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from trader.application.research.historical_screening import HistoricalArchiveManifest, HistoricalArchiveStatus
from trader.application.research.replay_models import canonical_hash
from trader.domain.research.score_r6 import (
    SCORE_R6_HISTORICAL_SPEC,
    ScoreR6Candidate,
    ScoreR6ProductionBoardWeights,
    ScoreR6ProductionCandidate,
)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REASON = re.compile(r"^[a-z0-9_]{1,64}$")
ScoreR6Board = Literal["main", "chinext", "star"]


@dataclass(frozen=True, order=True)
class ScoreR6HistoricalRow:
    trade_date: date
    code: str
    board: ScoreR6Board
    momentum_score: float
    stability_score: float
    liquidity_score: float
    volatility_20d_pct: float
    return_5d_pct: float

    def __post_init__(self) -> None:
        _code(self.code)
        if self.board not in {"main", "chinext", "star"}:
            raise ValueError("Score-R6 historical board is invalid")
        for value in (self.momentum_score, self.stability_score, self.liquidity_score):
            if not math.isfinite(value) or not 0.0 <= value <= 100.0:
                raise ValueError("Score-R6 reconstructed component must be in [0, 100]")
        if not math.isfinite(self.volatility_20d_pct) or self.volatility_20d_pct < 0.0:
            raise ValueError("Score-R6 reconstructed volatility must be finite and nonnegative")
        if not math.isfinite(self.return_5d_pct):
            raise ValueError("Score-R6 historical settlement must be finite")


@dataclass(frozen=True)
class ScoreR6Metrics:
    trade_dates: int
    selected_days: int
    pair_count: int
    mean_net_excess_5d_pct: float | None
    severe_loss_rate: float | None
    mean_turnover: float | None
    daily_net_excess_stddev: float | None
    oracle_recall: float | None
    maximum_stock_positive_contribution_fraction: float | None
    maximum_board_fraction: float | None
    objective_value: float | None
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if min(self.trade_dates, self.selected_days, self.pair_count) < 0 or self.selected_days > self.trade_dates:
            raise ValueError("Score-R6 metric counts are inconsistent")
        values = (
            self.mean_net_excess_5d_pct,
            self.severe_loss_rate,
            self.mean_turnover,
            self.daily_net_excess_stddev,
            self.oracle_recall,
            self.maximum_stock_positive_contribution_fraction,
            self.maximum_board_fraction,
            self.objective_value,
        )
        if any(value is not None and not math.isfinite(value) for value in values):
            raise ValueError("Score-R6 metrics must be finite when present")
        for value in (
            self.severe_loss_rate,
            self.mean_turnover,
            self.oracle_recall,
            self.maximum_stock_positive_contribution_fraction,
            self.maximum_board_fraction,
        ):
            if value is not None and not 0.0 <= value <= 1.0:
                raise ValueError("Score-R6 rate metrics must be in [0, 1]")
        object.__setattr__(self, "content_hash", canonical_hash(self))


@dataclass(frozen=True)
class ScoreR6FrozenCandidate:
    candidate: ScoreR6Candidate
    production_candidate: ScoreR6ProductionCandidate
    training_metrics_hash: str
    validation_metrics_hash: str
    training_objective: float
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.production_candidate.historical_candidate_hash != self.candidate.content_hash:
            raise ValueError("Score-R6 production candidate must bind the frozen historical candidate")
        _hash(self.training_metrics_hash)
        _hash(self.validation_metrics_hash)
        if not math.isfinite(self.training_objective):
            raise ValueError("Score-R6 training objective must be finite")
        object.__setattr__(self, "content_hash", canonical_hash(self))


@dataclass(frozen=True)
class ScoreR6BoardCandidate:
    board: ScoreR6Board
    source: Literal["board_fit", "global_fallback"]
    sample_rows: int
    sample_days: int
    candidate_hash: str
    candidate: ScoreR6Candidate
    production_weights: ScoreR6ProductionBoardWeights
    training_metrics_hash: str
    validation_metrics_hash: str
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.board not in {"main", "chinext", "star"} or min(self.sample_rows, self.sample_days) < 0:
            raise ValueError("Score-R6 board candidate sample identity is invalid")
        if self.source not in {"board_fit", "global_fallback"}:
            raise ValueError("Score-R6 board parameter source is invalid")
        for value in (self.candidate_hash, self.training_metrics_hash, self.validation_metrics_hash):
            _hash(value)
        if self.candidate_hash != self.candidate.content_hash or self.production_weights.board != self.board:
            raise ValueError("Score-R6 board candidate must bind its parameters and production weights")
        object.__setattr__(self, "content_hash", canonical_hash(self))


@dataclass(frozen=True)
class ScoreR6HistoricalReport:
    status: Literal["historical_screened", "insufficient_coverage", "historical_rejected"]
    research_identity: str
    research_spec_hash: str
    parent_archive: HistoricalArchiveStatus
    parent_manifest: HistoricalArchiveManifest
    global_candidate: ScoreR6FrozenCandidate | None
    forward_candidate: ScoreR6ProductionCandidate | None
    training: ScoreR6Metrics
    validation: ScoreR6Metrics
    baseline_validation: ScoreR6Metrics
    board_candidates: tuple[ScoreR6BoardCandidate, ...]
    historical_gate_passed: bool
    failure_reasons: tuple[str, ...]
    hybrid_increment_status: Literal["forward_required"]
    promotion_authority: Literal[False]
    limitations: tuple[str, ...]
    schema_version: str = "score_r6_historical_report_v1"
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _validate_historical_report_identity(self)
        reasons = tuple(sorted(set(self.failure_reasons)))
        _validate_historical_report_state(self, reasons)
        _validate_historical_report_candidates(self)
        object.__setattr__(self, "failure_reasons", reasons)
        object.__setattr__(self, "content_hash", canonical_hash(self))


@dataclass(frozen=True, order=True)
class ScoreR6ForwardPair:
    code: str
    board: ScoreR6Board
    production_weight: float
    local_weight: float
    hybrid_weight: float
    return_5d_pct: float
    severe_loss: bool

    def __post_init__(self) -> None:
        _code(self.code)
        if self.board not in {"main", "chinext", "star"}:
            raise ValueError("Score-R6 forward board is invalid")
        for value in (self.production_weight, self.local_weight, self.hybrid_weight):
            if not math.isfinite(value) or not 0.0 <= value <= 1.0:
                raise ValueError("Score-R6 forward weights must be in [0, 1]")
        if not math.isfinite(self.return_5d_pct):
            raise ValueError("Score-R6 forward settlement must be finite")


@dataclass(frozen=True)
class ScoreR6ForwardDay:
    research_spec_hash: str
    trade_date: date
    status: Literal["valid", "no_decision", "failed"]
    pairs: tuple[ScoreR6ForwardPair, ...]
    oracle_codes: tuple[str, ...]
    failure_reason: str | None
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _hash(self.research_spec_hash)
        if self.status not in {"valid", "no_decision", "failed"}:
            raise ValueError("Score-R6 forward day status is invalid")
        pairs = tuple(sorted(self.pairs, key=lambda item: item.code))
        if len({item.code for item in pairs}) != len(pairs):
            raise ValueError("Score-R6 forward pairs must be same-day unique stocks")
        if self.status == "failed":
            if (
                pairs
                or self.oracle_codes
                or self.failure_reason is None
                or _REASON.fullmatch(self.failure_reason) is None
            ):
                raise ValueError("failed Score-R6 day requires only a bounded reason")
        else:
            if not pairs or self.failure_reason is not None:
                raise ValueError("valid Score-R6 day requires pairs and no failure")
            pair_codes = {item.code for item in pairs}
            if len(set(self.oracle_codes)) != len(self.oracle_codes) or not set(self.oracle_codes).issubset(pair_codes):
                raise ValueError("Score-R6 oracle codes must be unique same-stock pairs")
            for label, weights in (
                ("production", tuple(item.production_weight for item in pairs)),
                ("local", tuple(item.local_weight for item in pairs)),
                ("hybrid", tuple(item.hybrid_weight for item in pairs)),
            ):
                total = math.fsum(weights)
                if total != 0.0 and not math.isclose(total, 1.0, rel_tol=0.0, abs_tol=1e-12):
                    raise ValueError(f"Score-R6 {label} weights must sum to one or zero")
            has_selection = any(item.local_weight > 0.0 or item.hybrid_weight > 0.0 for item in pairs)
            if (self.status == "valid") != has_selection:
                raise ValueError("Score-R6 day status must match local/hybrid selection")
        object.__setattr__(self, "pairs", pairs)
        object.__setattr__(self, "content_hash", canonical_hash(self))


@dataclass(frozen=True)
class ScoreR6ForwardReport:
    status: Literal["forward_collecting", "forward_rejected", "local_eligible", "hybrid_eligible"]
    research_identity: str
    research_spec_hash: str
    recorded_days: int
    pair_count: int
    day_hashes: tuple[str, ...]
    local_mean_gain_pct: float | None
    local_severe_rate_delta: float | None
    local_turnover_delta: float | None
    local_stability_delta: float | None
    local_recall: float | None
    local_maximum_stock_weight: float | None
    local_maximum_board_fraction: float | None
    hybrid_mean_increment_pct: float | None
    hybrid_confidence_lower_pct: float | None
    hybrid_p_value: float | None
    hybrid_bootstrap_seed: int | None
    local_gate_passed: bool
    hybrid_independent_gain_passed: bool
    production_scope: Literal["none", "local_only", "hybrid"]
    promotion_eligible: bool
    failure_reasons: tuple[str, ...]
    schema_version: str = "score_r6_forward_report_v1"
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _validate_forward_report_identity(self)
        _validate_forward_report_metrics(self)
        reasons = tuple(sorted(set(self.failure_reasons)))
        _validate_forward_report_state(self, reasons)
        object.__setattr__(self, "failure_reasons", reasons)
        object.__setattr__(self, "content_hash", canonical_hash(self))


def _validate_historical_report_identity(report: ScoreR6HistoricalReport) -> None:
    if report.status not in {"historical_screened", "insufficient_coverage", "historical_rejected"}:
        raise ValueError("Score-R6 historical report status is invalid")
    if (
        report.research_identity != SCORE_R6_HISTORICAL_SPEC.research_identity
        or report.research_spec_hash != SCORE_R6_HISTORICAL_SPEC.content_hash
        or report.schema_version != "score_r6_historical_report_v1"
    ):
        raise ValueError("Score-R6 historical report identity is invalid")
    _hash(report.research_spec_hash)
    if report.promotion_authority or report.hybrid_increment_status != "forward_required":
        raise ValueError("Score-R6 historical report cannot grant promotion or hybrid evidence")


def _validate_historical_report_state(report: ScoreR6HistoricalReport, reasons: tuple[str, ...]) -> None:
    if any(_REASON.fullmatch(reason) is None for reason in reasons):
        raise ValueError("Score-R6 failure reason is invalid")
    screened = report.status == "historical_screened"
    if screened and (report.global_candidate is None or report.forward_candidate is None):
        raise ValueError("screened Score-R6 report requires frozen historical and production candidates")
    if not screened and (report.global_candidate is not None or report.forward_candidate is not None or not reasons):
        raise ValueError("rejected Score-R6 report requires failures and no frozen candidate")
    if report.historical_gate_passed != (screened and not reasons):
        raise ValueError("Score-R6 historical gate must match its validation failures")
    if screened and tuple(item.board for item in report.board_candidates) != ("main", "chinext", "star"):
        raise ValueError("Score-R6 report must resolve all three boards")


def _validate_historical_report_candidates(report: ScoreR6HistoricalReport) -> None:
    if report.status != "historical_screened":
        return
    if report.global_candidate is None or report.forward_candidate is None:
        raise AssertionError("validated Score-R6 candidates unexpectedly missing")
    expected_boards = tuple(item.production_weights for item in report.board_candidates)
    if report.forward_candidate.boards != expected_boards:
        raise ValueError("Score-R6 forward candidate must bind the three resolved board candidates")
    if (
        report.forward_candidate.action_threshold != report.global_candidate.candidate.action_threshold
        or report.forward_candidate.risk_penalty != report.global_candidate.candidate.risk_penalty
    ):
        raise ValueError("Score-R6 forward candidate must bind the global threshold and risk penalty")


def _validate_forward_report_identity(report: ScoreR6ForwardReport) -> None:
    _hash(report.research_spec_hash)
    if report.status not in {"forward_collecting", "forward_rejected", "local_eligible", "hybrid_eligible"}:
        raise ValueError("Score-R6 forward report status is invalid")
    if report.production_scope not in {"none", "local_only", "hybrid"}:
        raise ValueError("Score-R6 forward production scope is invalid")
    if not report.research_identity.startswith("score_r6_forward_"):
        raise ValueError("Score-R6 forward report identity is invalid")
    if report.schema_version != "score_r6_forward_report_v1":
        raise ValueError("Score-R6 forward report schema is invalid")
    if min(report.recorded_days, report.pair_count) < 0:
        raise ValueError("Score-R6 forward report counts cannot be negative")
    if len(report.day_hashes) != report.recorded_days or any(
        _SHA256.fullmatch(value) is None for value in report.day_hashes
    ):
        raise ValueError("Score-R6 forward report must bind every recorded day hash")


def _validate_forward_report_metrics(report: ScoreR6ForwardReport) -> None:
    optional = (
        report.local_mean_gain_pct,
        report.local_severe_rate_delta,
        report.local_turnover_delta,
        report.local_stability_delta,
        report.local_recall,
        report.local_maximum_stock_weight,
        report.local_maximum_board_fraction,
        report.hybrid_mean_increment_pct,
        report.hybrid_confidence_lower_pct,
        report.hybrid_p_value,
    )
    if any(value is not None and not math.isfinite(value) for value in optional):
        raise ValueError("Score-R6 forward metrics must be finite")
    if report.hybrid_p_value is not None and not 0.0 <= report.hybrid_p_value <= 1.0:
        raise ValueError("Score-R6 hybrid p-value must be in [0, 1]")
    for value in (report.local_recall, report.local_maximum_stock_weight, report.local_maximum_board_fraction):
        if value is not None and not 0.0 <= value <= 1.0:
            raise ValueError("Score-R6 forward rates must be in [0, 1]")
    if report.hybrid_bootstrap_seed is not None and report.hybrid_bootstrap_seed < 0:
        raise ValueError("Score-R6 bootstrap seed cannot be negative")


def _validate_forward_report_state(report: ScoreR6ForwardReport, reasons: tuple[str, ...]) -> None:
    if any(_REASON.fullmatch(reason) is None for reason in reasons):
        raise ValueError("Score-R6 forward failure reason is invalid")
    bootstrap_present = report.hybrid_p_value is not None and report.hybrid_bootstrap_seed is not None
    if bootstrap_present != (report.recorded_days == 20 and report.hybrid_mean_increment_pct is not None):
        raise ValueError("Score-R6 hybrid bootstrap identity must match complete forward metrics")
    expected_scope = (
        "hybrid" if report.hybrid_independent_gain_passed else "local_only" if report.local_gate_passed else "none"
    )
    if report.production_scope != expected_scope or report.promotion_eligible != report.local_gate_passed:
        raise ValueError("Score-R6 eligibility must match its local and hybrid gates")
    expected_status = (
        "hybrid_eligible"
        if report.hybrid_independent_gain_passed
        else "local_eligible"
        if report.local_gate_passed
        else report.status
    )
    if report.status != expected_status:
        raise ValueError("Score-R6 forward status is inconsistent")


def _code(code: str) -> None:
    if len(code) != 6 or not code.isdigit():
        raise ValueError("Score-R6 stock code must contain six digits")


def _hash(value: str) -> None:
    if _SHA256.fullmatch(value) is None:
        raise ValueError("Score-R6 identity must be SHA-256")


__all__ = [
    "ScoreR6Board",
    "ScoreR6BoardCandidate",
    "ScoreR6ForwardDay",
    "ScoreR6ForwardPair",
    "ScoreR6ForwardReport",
    "ScoreR6FrozenCandidate",
    "ScoreR6HistoricalReport",
    "ScoreR6HistoricalRow",
    "ScoreR6Metrics",
]
