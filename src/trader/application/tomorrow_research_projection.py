"""Project an existing tomorrow decision into privacy-bounded research evidence."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from typing import Literal

from trader.application.ports.tomorrow_research import (
    TomorrowResearchTraceEnqueueResult,
    TomorrowResearchTraceRecorderPort,
)
from trader.application.tomorrow_research_trace_types import (
    TomorrowCandidateResearchTrace,
    TomorrowDecisionCandidateTrace,
    TomorrowDecisionSetTrace,
    TomorrowHardFilterAggregate,
    TomorrowResearchTrace,
    TomorrowResearchTraceCapture,
)
from trader.application.tomorrow_shadow_projection import TomorrowShadowProjection
from trader.domain.recommendation.downside import assess_downside
from trader.domain.recommendation.models import FusionMode, Strategy
from trader.domain.recommendation.scoring import candidate_fields
from trader.domain.recommendation.tomorrow_fusion import DecisionEpoch, TomorrowDecisionEntry
from trader.domain.recommendation.tomorrow_selection import TomorrowDisposition, TomorrowStockEvaluation


def build_tomorrow_research_trace(
    projection: TomorrowShadowProjection,
    *,
    baseline_snapshot_id: str,
) -> TomorrowResearchTrace:
    evaluations = projection.selection.evaluations
    hard_filter_aggregates = _hard_filter_aggregates(evaluations)
    population_by_board = tuple(sorted(Counter(item.features.quote.board.value for item in evaluations).items()))
    passed_candidates = tuple(
        _candidate_trace(item, production_candidate_codes=projection.production_candidate_codes)
        for item in evaluations
        if item.disposition is not TomorrowDisposition.REJECT
    )
    shadow_epoch = projection.hybrid or projection.local
    return TomorrowResearchTrace(
        evaluated_at=projection.received_at,
        trade_date=projection.local.trade_date,
        phase=projection.phase,
        input_version=projection.input_version,
        input_manifest_hash=_sha256_text(projection.input_version),
        data_version=projection.data_version,
        config_version=projection.local.config_version,
        rule_versions=tuple(sorted({item.features.quote.rule_version or "unavailable" for item in evaluations})),
        hard_filter_aggregate_hash=_aggregate_hash(population_by_board, hard_filter_aggregates),
        received_population_by_board=population_by_board,
        hard_filter_aggregates=hard_filter_aggregates,
        source_coverage_status=projection.input_quality.status,
        source_failure_categories=projection.input_quality.degraded_reasons,
        passed_candidates=passed_candidates,
        production_local=_decision_set(projection.local, "production_local"),
        research_shadow=_decision_set(shadow_epoch, "research_shadow"),
        shadow_mode="reused_facts" if projection.hybrid is not None else "control_copy",
        baseline_snapshot_id=baseline_snapshot_id,
        deepseek_request_delta=0,
    )


def capture_tomorrow_research_trace(
    recorder: TomorrowResearchTraceRecorderPort | None,
    projection: TomorrowShadowProjection,
    *,
    baseline_snapshot_id: str,
) -> TomorrowResearchTraceEnqueueResult | None:
    if recorder is None:
        return None
    try:
        return recorder.enqueue(
            TomorrowResearchTraceCapture(
                projection,
                baseline_snapshot_id,
            )
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return None


def _candidate_trace(
    evaluation: TomorrowStockEvaluation,
    *,
    production_candidate_codes: frozenset[str],
) -> TomorrowCandidateResearchTrace:
    feature = evaluation.features
    required_fields = candidate_fields(Strategy.TOMORROW)
    missing_mask = tuple(name for name in required_fields if feature.optional_value(name) is None)
    return TomorrowCandidateResearchTrace(
        code=evaluation.code,
        board=feature.quote.board.value,
        industry=feature.quote.industry.strip() or "unknown",
        feature_input_hash=_feature_input_hash(evaluation),
        candidate_components=tuple(evaluation.candidate_components.items()),
        missing_mask=missing_mask,
        coverage_ratio=round(1.0 - feature.missing_ratio(required_fields), 6),
        board_reliability=round(feature.board_data_reliability, 6),
        candidate_score=evaluation.candidate_score,
        candidate_rank=evaluation.candidate_audit_rank,
        production_top120=evaluation.code in production_candidate_codes,
        optimistic_upper_bound=None,
        upper_bound_status="not_computed",
        upper_bound_protected=False,
        pruning_reason=(
            evaluation.selection_skip_reason
            if evaluation.code in production_candidate_codes
            else evaluation.candidate_audit_pruning_reason or "production_preselection_excluded"
        ),
    )


def _decision_set(
    epoch: DecisionEpoch,
    variant: Literal["production_local", "research_shadow"],
) -> TomorrowDecisionSetTrace:
    return TomorrowDecisionSetTrace(
        variant=variant,
        decision_version=epoch.version,
        schema_version=epoch.schema_version,
        strategy_version=epoch.strategy_version,
        fusion_version=epoch.fusion_version,
        candidates=tuple(_decision_candidate(item) for item in epoch.entries),
    )


def _decision_candidate(entry: TomorrowDecisionEntry) -> TomorrowDecisionCandidateTrace:
    score = entry.score
    downside = assess_downside(entry.features, Strategy.TOMORROW)
    return TomorrowDecisionCandidateTrace(
        code=entry.code,
        components=tuple(score.components.items()),
        component_coverage_ratio=round(entry.features.board_supported_weight, 6),
        base_score=score.base_score,
        local_risk_codes=tuple(fact.risk_code for fact in entry.local_risk_facts),
        local_risk_penalty=score.local_risk_penalty,
        local_score=score.local_score,
        reused_deepseek_facts=entry.review is not None,
        fusion_applied=score.fusion_mode is FusionMode.HYBRID and score.fusion_applied,
        deepseek_risk_codes=tuple(fact.risk_code for fact in entry.deepseek_risk_facts),
        deepseek_risk_penalty=score.deepseek_risk_penalty,
        final_score=score.final_score,
        action=entry.action.value,
        downside_status=downside.status,
        downside_reasons=downside.reasons,
        setup_type=downside.setup_type,
        selected=entry.selected,
        rank=entry.rank,
        board_rank=entry.board_rank,
        skip_reason=entry.decision_skip_reason or entry.local_selection_skip_reason,
    )


def _hard_filter_aggregates(
    evaluations: tuple[TomorrowStockEvaluation, ...],
) -> tuple[TomorrowHardFilterAggregate, ...]:
    counts: Counter[tuple[str, str]] = Counter()
    for evaluation in evaluations:
        if evaluation.disposition is not TomorrowDisposition.REJECT:
            continue
        board = evaluation.features.quote.board.value
        counts.update((board, reason.code) for reason in evaluation.filter_reasons)
    return tuple(TomorrowHardFilterAggregate(board, reason, count) for (board, reason), count in sorted(counts.items()))


def _feature_input_hash(evaluation: TomorrowStockEvaluation) -> str:
    feature = evaluation.features
    population = feature.board_population
    payload = {
        "code": evaluation.code,
        "merge_epoch": feature.merge_epoch,
        "observed_at": feature.observed_at.isoformat(),
        "quote_data_version": feature.quote.data_version,
        "quote_source_time": feature.quote.source_time.isoformat(),
        "board_policy_version": feature.board_policy_version,
        "population_version": population.population_version if population is not None else "",
        "evidence": tuple((item.evidence_id, item.data_version) for item in feature.evidence),
    }
    return _sha256_json(payload)


def _aggregate_hash(
    populations: tuple[tuple[str, int], ...],
    aggregates: tuple[TomorrowHardFilterAggregate, ...],
) -> str:
    return _sha256_json(
        {
            "populations": populations,
            "aggregates": tuple((item.board, item.reason, item.count) for item in aggregates),
        }
    )


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha256_json(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return _sha256_text(payload)


__all__ = ["build_tomorrow_research_trace", "capture_tomorrow_research_trace"]
