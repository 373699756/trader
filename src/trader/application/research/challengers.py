"""Score-R4 orchestration for five isolated historical challenger replays."""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Callable
from datetime import date
from typing import Literal

from trader.application.research.challenger_models import (
    ChallengerCandidateOverride,
    ChallengerDayReplay,
    ChallengerReplaySelection,
    ChallengerSameStockPair,
    ChallengerVariantReplay,
    ScoreR4ChallengerReport,
)
from trader.application.research.models import (
    HistoricalEvaluatedCandidate,
    HistoricalExtractedDay,
    ScoreR2HistoricalExtraction,
)
from trader.application.research.ports import HistoricalChallengerReplayEvaluator
from trader.application.research.replay_models import BaselineDayMetrics, ScoreR3BaselineReport, canonical_hash
from trader.domain.research.challengers import (
    ChallengerSpecification,
    ContinuousEntryInputs,
    HeatWeakStructureInputs,
    assess_continuous_entry,
    assess_heat_weak_structure,
    challenger_parameter_manifest,
    challenger_registry,
)
from trader.domain.research.historical import coverage_shrunk_score

_MAXIMUM_PER_BOARD = 4
_MAXIMUM_PER_INDUSTRY = 2


class ScoreR4ChallengerReplayer:
    """Build immutable overrides and delegate every score/selection replay to production functions."""

    def __init__(self, evaluator: HistoricalChallengerReplayEvaluator) -> None:
        self._evaluator = evaluator

    def replay(
        self,
        extraction: ScoreR2HistoricalExtraction,
        baseline: ScoreR3BaselineReport,
    ) -> ScoreR4ChallengerReport:
        _validate_parent_reports(extraction, baseline)
        parameter_hash = canonical_hash(challenger_parameter_manifest())
        baseline_by_date = {item.trade_date: item for item in baseline.days}
        variants = tuple(
            self._replay_variant(specification, extraction.days, baseline_by_date, parameter_hash)
            for specification in challenger_registry()
        )
        return ScoreR4ChallengerReport(
            "replayed" if extraction.status == "extracted" and len(extraction.days) == 40 else "exploratory",
            extraction.content_hash,
            baseline.report_hash,
            parameter_hash,
            variants,
            extraction.research_identity,
            extraction.research_spec_hash,
            schema_version=(
                "score_r4_challenger_replay_candidate"
                if extraction.research_identity == "score_p0_v2"
                else "score_r4_challenger_replay_baseline"
            ),
        )

    def _replay_variant(
        self,
        specification: ChallengerSpecification,
        days: tuple[HistoricalExtractedDay, ...],
        baseline_by_date: dict[date, BaselineDayMetrics],
        parameter_hash: str,
    ) -> ChallengerVariantReplay:
        replayed = tuple(self._replay_day(day, baseline_by_date[day.summary.trade_date], specification) for day in days)
        return ChallengerVariantReplay(
            specification.variant_id,
            specification.variant_version,
            parameter_hash,
            replayed,
        )

    def _replay_day(
        self,
        day: HistoricalExtractedDay,
        baseline: BaselineDayMetrics,
        specification: ChallengerSpecification,
    ) -> ChallengerDayReplay:
        overrides = _build_overrides(day, specification)
        selections = self._evaluator.replay(day, specification, overrides)
        _validate_selections(day, baseline, overrides, selections)
        production_count = sum(item.production_rank is not None for item in selections)
        local_count = sum(item.local_rank is not None for item in selections)
        hybrid_count = sum(item.hybrid_rank is not None for item in selections)
        basis_by_code = {item.basis.code: item.basis for item in day.full_fields.settlements}
        evaluated_by_code = {item.code: item for item in day.evaluated}
        pairs = tuple(
            ChallengerSameStockPair(
                item.code,
                evaluated_by_code[item.code].board,
                item.production_rank,
                item.local_rank,
                item.hybrid_rank,
                _weight(item.production_rank, production_count),
                _weight(item.local_rank, local_count),
                _weight(item.hybrid_rank, hybrid_count),
                item.local_score,
                item.hybrid_score,
                item.hybrid_source,
                basis_by_code[item.code],
            )
            for item in selections
        )
        return ChallengerDayReplay(
            day.summary.trade_date,
            day.content_hash,
            day.summary.input_hash,
            overrides,
            pairs,
            "selected" if local_count else "no_decision",
            "selected" if hybrid_count else "no_decision",
        )


def _build_overrides(
    day: HistoricalExtractedDay,
    specification: ChallengerSpecification,
) -> tuple[ChallengerCandidateOverride, ...]:
    summaries = {item.code: item for item in day.summary.candidates}
    full = {item.code: item for item in day.full_fields.candidates}
    proofs = {(item.code, item.pool): item for item in day.proofs}
    overrides: list[ChallengerCandidateOverride] = []
    for evaluated in day.evaluated:
        summary = summaries[evaluated.code]
        payload = full[evaluated.code].payload
        observe_reasons: list[str] = []
        entry_score: float | None = None
        entry_status: Literal["not_enabled", "scored", "critical_missing"] = "not_enabled"
        if specification.continuous_entry:
            entry = assess_continuous_entry(
                ContinuousEntryInputs(
                    _number(payload.get("price")),
                    _number(payload.get("ma5")),
                    _number(payload.get("ma10")),
                    _number(payload.get("ma20")),
                    _number(payload.get("ma20_slope_pct")),
                    _number(payload.get("volume_to_5d_average")),
                    _number(payload.get("prior_high_20d")),
                    _number(payload.get("breakout_deviation_pct")),
                    _number(payload.get("close_location")),
                )
            )
            entry_score = entry.score
            entry_status = entry.status
            if entry.status == "critical_missing":
                observe_reasons.append("continuous_entry_inputs_missing")
        coverage_score = coverage_shrunk_score(summary.final_components) if specification.coverage_shrink else None
        proof = proofs.get((evaluated.code, "formal"))
        expanded = bool(
            specification.candidate_upper_bound
            and not summary.production_top120
            and "formal" in summary.eligible_pools
            and summary.production_candidate_score >= 50.0
            and summary.candidate_core_missing_ratio <= 0.30
            and proof is not None
            and proof.status == "loaded"
        )
        selection_eligible = "formal" in summary.eligible_pools and (summary.production_top120 or expanded)
        if specification.heat_weak_structure:
            heat = assess_heat_weak_structure(
                evaluated.board,
                HeatWeakStructureInputs(
                    _number(payload.get("change_pct")),
                    _number(payload.get("close_location")),
                    _number(payload.get("tail_return_30m_pct")),
                    _number(payload.get("intraday_drawdown_pct")),
                ),
            )
            observe_reasons.extend(heat.reasons)
        overrides.append(
            ChallengerCandidateOverride(
                evaluated.code,
                entry_score,
                entry_status,
                coverage_score,
                expanded,
                selection_eligible,
                bool(observe_reasons),
                tuple(observe_reasons),
            )
        )
    return tuple(overrides)


def _validate_parent_reports(
    extraction: ScoreR2HistoricalExtraction,
    baseline: ScoreR3BaselineReport,
) -> None:
    if baseline.extraction_hash != extraction.content_hash:
        raise ValueError("Score-R4 baseline must bind the same R2 extraction")
    baseline_days = tuple((item.trade_date, item.day_hash, item.input_hash) for item in baseline.days)
    extraction_days = tuple(
        (item.summary.trade_date, item.content_hash, item.summary.input_hash) for item in extraction.days
    )
    if baseline_days != extraction_days:
        raise ValueError("Score-R4 baseline days must match the R2 extraction")


def _validate_selections(
    day: HistoricalExtractedDay,
    baseline: BaselineDayMetrics,
    overrides: tuple[ChallengerCandidateOverride, ...],
    selections: tuple[ChallengerReplaySelection, ...],
) -> None:
    expected_codes = tuple(item.code for item in day.evaluated)
    if tuple(item.code for item in selections) != expected_codes:
        raise ValueError("Score-R4 evaluator must preserve exact active-set code order")
    override_by_code = {item.code: item for item in overrides}
    summary_by_code = {item.code: item for item in day.summary.candidates}
    evaluated_by_code = {item.code: item for item in day.evaluated}
    baseline_ranks = {code: rank for rank, code in enumerate(baseline.selected_codes, start=1)}
    if any(item.production_rank != baseline_ranks.get(item.code) for item in selections):
        raise ValueError("Score-R4 production ranks must equal the frozen Score-R3 baseline")
    for item in selections:
        override = override_by_code[item.code]
        summary = summary_by_code[item.code]
        if (item.local_rank is not None or item.hybrid_rank is not None) and (
            not override.selection_eligible or override.force_observe_only
        ):
            raise ValueError("Score-R4 challenger selected an ineligible or observe-only candidate")
        expected_source = "existing_facts" if summary.recorded_deepseek_score is not None else "control_copy"
        if item.hybrid_source != expected_source:
            raise ValueError("Score-R4 hybrid may only apply existing facts or an exact local control copy")
    _validate_ranked_pool(selections, evaluated_by_code, lambda item: item.production_rank, "production")
    _validate_ranked_pool(selections, evaluated_by_code, lambda item: item.local_rank, "local")
    _validate_ranked_pool(selections, evaluated_by_code, lambda item: item.hybrid_rank, "hybrid")
    if all(item.hybrid_source == "control_copy" for item in selections) and any(
        item.hybrid_rank != item.local_rank for item in selections
    ):
        raise ValueError("Score-R4 all-control hybrid selection must equal local-only")


def _validate_ranked_pool(
    selections: tuple[ChallengerReplaySelection, ...],
    evaluated_by_code: dict[str, HistoricalEvaluatedCandidate],
    rank_getter: Callable[[ChallengerReplaySelection], int | None],
    label: str,
) -> None:
    selected = tuple(item for item in selections if rank_getter(item) is not None)
    ranks = sorted(rank for item in selected if (rank := rank_getter(item)) is not None)
    if ranks != list(range(1, len(selected) + 1)) or len(selected) > 6:
        raise ValueError(f"Score-R4 {label} ranks must be contiguous Top6")
    if not selected:
        return
    if label == "production":
        expected = tuple(
            item.code
            for item in sorted(
                selected,
                key=lambda item: (
                    -evaluated_by_code[item.code].final_score,
                    -evaluated_by_code[item.code].local_score,
                    item.code,
                ),
            )
        )
    elif label == "local":
        expected = tuple(item.code for item in sorted(selected, key=lambda item: (-item.local_score, item.code)))
    else:
        expected = tuple(
            item.code for item in sorted(selected, key=lambda item: (-item.hybrid_score, -item.local_score, item.code))
        )
    actual = tuple(item.code for item in sorted(selected, key=lambda item: rank_getter(item) or 0))
    if expected != actual:
        raise ValueError(f"Score-R4 {label} ranks must use the production stable score order")
    boards = Counter(evaluated_by_code[item.code].board for item in selected)
    industries = Counter(evaluated_by_code[item.code].industry or "unknown" for item in selected)
    if any(count > _MAXIMUM_PER_BOARD for count in boards.values()):
        raise ValueError(f"Score-R4 {label} exceeds the board concentration limit")
    if any(count > _MAXIMUM_PER_INDUSTRY for count in industries.values()):
        raise ValueError(f"Score-R4 {label} exceeds the industry concentration limit")


def _weight(rank: int | None, selected_count: int) -> float:
    return 0.0 if rank is None else 1.0 / selected_count


def _number(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Score-R4 numeric payload fields must be numbers or null")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("Score-R4 numeric payload fields must be finite")
    return number


__all__ = ["ScoreR4ChallengerReplayer"]
