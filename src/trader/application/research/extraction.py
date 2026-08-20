"""Deterministic two-phase extraction for the preregistered Score-R2 window."""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from typing import Literal

from trader.application.research.models import (
    HistoricalCandidateProof,
    HistoricalCoverageRecord,
    HistoricalEvaluatedCandidate,
    HistoricalExtractedDay,
    ScoreR2HistoricalExtraction,
)
from trader.application.research.ports import HistoricalCandidateEvaluator, HistoricalDataPlaneReadPort
from trader.domain.research.historical import (
    HistoricalCandidateSummary,
    ResearchSelectionPool,
    optimistic_component_upper_bound,
    optimistic_final_upper_bound,
)
from trader.domain.research.specification import SCORE_P0_V1_SPEC, ScoreResearchSpec

_PROOF_RULE_VERSION = "score_r2_active_set_v1"


@dataclass(frozen=True)
class ScoreR2ExtractionPolicy:
    top_k: int = 6
    maximum_board_fraction: float = 0.6
    maximum_per_industry: int = 2

    def __post_init__(self) -> None:
        if not 1 <= self.top_k <= 6:
            raise ValueError("Score-R2 TopK must be in [1, 6]")
        if not 0.0 < self.maximum_board_fraction <= 1.0 or self.maximum_per_industry < 1:
            raise ValueError("Score-R2 concentration limits are invalid")


class ScoreR2HistoricalExtractor:
    """Read only point-in-time evidence and retain a provably sufficient active set."""

    def __init__(
        self,
        data_plane: HistoricalDataPlaneReadPort,
        evaluator: HistoricalCandidateEvaluator,
        *,
        policy: ScoreR2ExtractionPolicy | None = None,
        spec: ScoreResearchSpec = SCORE_P0_V1_SPEC,
    ) -> None:
        self._data_plane = data_plane
        self._evaluator = evaluator
        self._policy = policy or ScoreR2ExtractionPolicy()
        self._spec = spec
        self._maximum_board_count = math.ceil(self._policy.top_k * self._policy.maximum_board_fraction)

    def extract(self) -> ScoreR2HistoricalExtraction:
        coverage: list[HistoricalCoverageRecord] = []
        days: list[HistoricalExtractedDay] = []
        for trade_date in self._spec.historical_dates:
            self._attempt_date(trade_date, coverage, days)
        for trade_date in self._spec.historical_replacement_dates:
            if len(days) >= self._spec.maximum_historical_days:
                break
            self._attempt_date(trade_date, coverage, days)
        if len(days) > self._spec.maximum_historical_days:
            days = sorted(days, key=lambda item: item.summary.trade_date)[-self._spec.maximum_historical_days :]
            retained = {item.summary.trade_date for item in days}
            coverage = [item for item in coverage if item.status == "failed" or item.trade_date in retained]
        return ScoreR2HistoricalExtraction(
            status="extracted" if len(days) == self._spec.maximum_historical_days else "exploratory",
            coverage=tuple(coverage),
            days=tuple(days),
            research_identity=self._spec.research_identity,
            research_spec_hash=self._spec.content_hash,
            schema_version=(
                "score_r2_historical_v2" if self._spec.research_identity == "score_p0_v2" else "score_r2_historical_v1"
            ),
        )

    def _attempt_date(
        self,
        trade_date: date,
        coverage: list[HistoricalCoverageRecord],
        days: list[HistoricalExtractedDay],
    ) -> None:
        if len(days) >= self._spec.maximum_historical_days or not self._data_plane.is_trading_day(trade_date):
            return
        try:
            day = self._extract_day(trade_date)
        except (LookupError, RuntimeError, TypeError, ValueError) as exc:
            coverage.append(HistoricalCoverageRecord(trade_date, "failed", _coverage_reason(exc)))
            return
        coverage.append(HistoricalCoverageRecord(trade_date, "valid", "complete", day.content_hash))
        days.append(day)

    def _extract_day(self, trade_date: date) -> HistoricalExtractedDay:
        summary = self._data_plane.read_day_summary(trade_date)
        if summary.trade_date != trade_date:
            raise ValueError("historical summary trade date mismatch")
        loaded_codes = set(_production_codes(summary.candidates))
        evaluated: tuple[HistoricalEvaluatedCandidate, ...] = ()
        full_fields = self._data_plane.load_full_fields(trade_date, tuple(sorted(loaded_codes)))
        while True:
            evaluated = self._evaluator.evaluate(summary, full_fields)
            _validate_evaluations(evaluated, loaded_codes)
            candidate = self._next_protected_candidate(summary.candidates, evaluated, loaded_codes)
            if candidate is None:
                break
            loaded_codes.add(candidate.code)
            full_fields = self._data_plane.load_full_fields(trade_date, tuple(sorted(loaded_codes)))
        proofs = _proofs(
            summary.candidates,
            evaluated,
            loaded_codes,
            policy=self._policy,
            maximum_board_count=self._maximum_board_count,
        )
        return HistoricalExtractedDay(summary, full_fields, evaluated, proofs)

    def _next_protected_candidate(
        self,
        candidates: tuple[HistoricalCandidateSummary, ...],
        evaluated: tuple[HistoricalEvaluatedCandidate, ...],
        loaded_codes: set[str],
    ) -> HistoricalCandidateSummary | None:
        unloaded = sorted(
            (
                candidate
                for candidate in candidates
                if candidate.code not in loaded_codes
                and candidate.production_candidate_score >= 50.0
                and candidate.candidate_core_missing_ratio <= 0.30
                and _candidate_upper_bound(candidate) >= 50.0
            ),
            key=lambda candidate: (-_upper_bound(candidate), candidate.code),
        )
        for candidate in unloaded:
            if any(
                _can_enter(
                    candidate,
                    pool,
                    evaluated,
                    policy=self._policy,
                    maximum_board_count=self._maximum_board_count,
                )
                for pool in candidate.eligible_pools
            ):
                return candidate
        return None


def _production_codes(
    candidates: tuple[HistoricalCandidateSummary, ...],
) -> tuple[str, ...]:
    counts: dict[str, int] = defaultdict(int)
    for candidate in candidates:
        if candidate.production_top120:
            counts[candidate.board] += 1
    if any(count > 120 for count in counts.values()):
        raise ValueError("historical production Top120 identity exceeds its board limit")
    return tuple(sorted(candidate.code for candidate in candidates if candidate.production_top120))


def _upper_bound(candidate: HistoricalCandidateSummary) -> float:
    return optimistic_final_upper_bound(
        candidate.final_components,
        mandatory_local_risk_penalty=candidate.mandatory_local_risk_penalty,
        recorded_deepseek_score=candidate.recorded_deepseek_score,
        recorded_deepseek_risk_penalty=candidate.recorded_deepseek_risk_penalty,
    )


def _candidate_upper_bound(candidate: HistoricalCandidateSummary) -> float:
    return optimistic_component_upper_bound(candidate.candidate_components)


def _can_enter(
    candidate: HistoricalCandidateSummary,
    pool: ResearchSelectionPool,
    evaluated: tuple[HistoricalEvaluatedCandidate, ...],
    *,
    policy: ScoreR2ExtractionPolicy,
    maximum_board_count: int,
) -> bool:
    optimistic = HistoricalEvaluatedCandidate(
        candidate.code,
        candidate.board,
        candidate.industry,
        _upper_bound(candidate),
        _upper_bound(candidate),
        (pool,),
    )
    selected = _select_pool(
        (*evaluated, optimistic),
        pool,
        top_k=policy.top_k,
        maximum_board_count=maximum_board_count,
        maximum_per_industry=policy.maximum_per_industry,
    )
    return candidate.code in {item.code for item in selected}


def _select_pool(
    evaluated: tuple[HistoricalEvaluatedCandidate, ...],
    pool: ResearchSelectionPool,
    *,
    top_k: int,
    maximum_board_count: int,
    maximum_per_industry: int,
) -> tuple[HistoricalEvaluatedCandidate, ...]:
    ordered = sorted(
        (item for item in evaluated if pool in item.eligible_pools),
        key=lambda item: (-item.final_score, -item.local_score, item.code),
    )
    selected: list[HistoricalEvaluatedCandidate] = []
    board_counts: dict[str, int] = defaultdict(int)
    industry_counts: dict[str, int] = defaultdict(int)
    for item in ordered:
        if len(selected) >= top_k:
            break
        if board_counts[item.board] >= maximum_board_count:
            continue
        industry = item.industry or "unknown"
        if industry_counts[industry] >= maximum_per_industry:
            continue
        selected.append(item)
        board_counts[item.board] += 1
        industry_counts[industry] += 1
    return tuple(selected)


def _proofs(
    candidates: tuple[HistoricalCandidateSummary, ...],
    evaluated: tuple[HistoricalEvaluatedCandidate, ...],
    loaded_codes: set[str],
    *,
    policy: ScoreR2ExtractionPolicy,
    maximum_board_count: int,
) -> tuple[HistoricalCandidateProof, ...]:
    frontiers = {
        pool: _frontier(
            evaluated,
            pool,
            top_k=policy.top_k,
            maximum_board_count=maximum_board_count,
            maximum_per_industry=policy.maximum_per_industry,
        )
        for pool in ("formal", "observation")
    }
    proofs: list[HistoricalCandidateProof] = []
    for candidate in candidates:
        for pool in candidate.eligible_pools:
            upper_bound = _upper_bound(candidate)
            loaded = candidate.code in loaded_codes
            reason: Literal["active_set_loaded", "upper_bound_below_frontier", "selection_constraint"] = (
                "active_set_loaded"
            )
            if not loaded:
                reason = "upper_bound_below_frontier" if upper_bound < frontiers[pool] else "selection_constraint"
            proofs.append(
                HistoricalCandidateProof(
                    candidate.code,
                    pool,
                    upper_bound,
                    frontiers[pool],
                    "loaded" if loaded else "excluded",
                    reason,
                    _PROOF_RULE_VERSION,
                )
            )
    return tuple(proofs)


def _frontier(
    evaluated: tuple[HistoricalEvaluatedCandidate, ...],
    pool: ResearchSelectionPool,
    *,
    top_k: int,
    maximum_board_count: int,
    maximum_per_industry: int,
) -> float:
    selected = _select_pool(
        evaluated,
        pool,
        top_k=top_k,
        maximum_board_count=maximum_board_count,
        maximum_per_industry=maximum_per_industry,
    )
    return min((item.final_score for item in selected), default=0.0) if len(selected) == top_k else 0.0


def _validate_evaluations(evaluated: tuple[HistoricalEvaluatedCandidate, ...], loaded_codes: set[str]) -> None:
    codes = tuple(item.code for item in evaluated)
    if len(codes) != len(set(codes)) or set(codes) != loaded_codes:
        raise ValueError("historical evaluator must exactly cover the active set")


def _coverage_reason(exc: Exception) -> str:
    if isinstance(exc, LookupError):
        return "point_in_time_data_missing"
    if isinstance(exc, RuntimeError):
        return "point_in_time_read_failed"
    if isinstance(exc, TypeError):
        return "point_in_time_schema_invalid"
    return "point_in_time_evidence_invalid"


__all__ = ["ScoreR2ExtractionPolicy", "ScoreR2HistoricalExtractor"]
