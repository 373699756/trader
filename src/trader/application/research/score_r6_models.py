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
    validated_candidate: ScoreR6ProductionCandidate | None
    training: ScoreR6Metrics
    validation: ScoreR6Metrics
    baseline_validation: ScoreR6Metrics
    board_candidates: tuple[ScoreR6BoardCandidate, ...]
    historical_gate_passed: bool
    failure_reasons: tuple[str, ...]
    validation_mode: Literal["historical_only"]
    promotion_authority: Literal[False]
    limitations: tuple[str, ...]
    schema_version: str = "score_r6_historical_report"
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _validate_historical_report_identity(self)
        reasons = tuple(sorted(set(self.failure_reasons)))
        _validate_historical_report_state(self, reasons)
        _validate_historical_report_candidates(self)
        object.__setattr__(self, "failure_reasons", reasons)
        object.__setattr__(self, "content_hash", canonical_hash(self))


def _validate_historical_report_identity(report: ScoreR6HistoricalReport) -> None:
    if report.status not in {"historical_screened", "insufficient_coverage", "historical_rejected"}:
        raise ValueError("Score-R6 historical report status is invalid")
    if (
        report.research_identity != SCORE_R6_HISTORICAL_SPEC.research_identity
        or report.research_spec_hash != SCORE_R6_HISTORICAL_SPEC.content_hash
        or report.schema_version != "score_r6_historical_report"
    ):
        raise ValueError("Score-R6 historical report identity is invalid")
    _hash(report.research_spec_hash)
    if report.promotion_authority or report.validation_mode != "historical_only":
        raise ValueError("Score-R6 report must remain historical-only and non-production")


def _validate_historical_report_state(report: ScoreR6HistoricalReport, reasons: tuple[str, ...]) -> None:
    if any(_REASON.fullmatch(reason) is None for reason in reasons):
        raise ValueError("Score-R6 failure reason is invalid")
    screened = report.status == "historical_screened"
    if screened and (report.global_candidate is None or report.validated_candidate is None):
        raise ValueError("screened Score-R6 report requires frozen historical and production candidates")
    if not screened and (report.global_candidate is not None or report.validated_candidate is not None or not reasons):
        raise ValueError("rejected Score-R6 report requires failures and no frozen candidate")
    if report.historical_gate_passed != (screened and not reasons):
        raise ValueError("Score-R6 historical gate must match its validation failures")
    if screened and tuple(item.board for item in report.board_candidates) != ("main", "chinext", "star"):
        raise ValueError("Score-R6 report must resolve all three boards")


def _validate_historical_report_candidates(report: ScoreR6HistoricalReport) -> None:
    if report.status != "historical_screened":
        return
    if report.global_candidate is None or report.validated_candidate is None:
        raise AssertionError("validated Score-R6 candidates unexpectedly missing")
    expected_boards = tuple(item.production_weights for item in report.board_candidates)
    if report.validated_candidate.boards != expected_boards:
        raise ValueError("Score-R6 validated candidate must bind the three resolved board candidates")
    if (
        report.validated_candidate.action_threshold != report.global_candidate.candidate.action_threshold
        or report.validated_candidate.risk_penalty != report.global_candidate.candidate.risk_penalty
    ):
        raise ValueError("Score-R6 validated candidate must bind the global threshold and risk penalty")


def _code(code: str) -> None:
    if len(code) != 6 or not code.isdigit():
        raise ValueError("Score-R6 stock code must contain six digits")


def _hash(value: str) -> None:
    if _SHA256.fullmatch(value) is None:
        raise ValueError("Score-R6 identity must be SHA-256")


__all__ = [
    "ScoreR6Board",
    "ScoreR6BoardCandidate",
    "ScoreR6FrozenCandidate",
    "ScoreR6HistoricalReport",
    "ScoreR6HistoricalRow",
    "ScoreR6Metrics",
]
