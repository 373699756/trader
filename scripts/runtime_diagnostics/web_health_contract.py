"""Typed parsing boundary for sanitized 统一 Web-health samples."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

_MAX_REASON_COUNTS = 32


@dataclass(frozen=True)
class FetchIssue:
    endpoint: str
    error_code: str


@dataclass(frozen=True)
class PipelineFacetSnapshot:
    key: str | None
    count: int | None
    total: int | None


@dataclass(frozen=True)
class PipelineStageSnapshot:
    key: str | None
    state: str | None
    input_count: int | None
    output_count: int | None
    facets: tuple[PipelineFacetSnapshot, ...]
    invalid_fields: tuple[str, ...] = ()

    def facet_count(self, key: str) -> int | None:
        return next((facet.count for facet in self.facets if facet.key == key), None)


@dataclass(frozen=True)
class PipelineSnapshot:
    current_stage: str | None
    stages: tuple[PipelineStageSnapshot, ...]
    invalid_fields: tuple[str, ...] = ()

    def stage(self, key: str) -> PipelineStageSnapshot | None:
        return next((stage for stage in self.stages if stage.key == key), None)

    def count(self, name: str) -> int | None:
        stage_key, source = _PIPELINE_COUNT_LOCATIONS[name]
        stage = self.stage(stage_key)
        if stage is None:
            return None
        if source == "input_count":
            return stage.input_count
        if source == "output_count":
            return stage.output_count
        return stage.facet_count(source)

    def monitored_counts(self) -> tuple[tuple[str, int | None], ...]:
        return tuple((name, self.count(name)) for name in _MONITORED_PIPELINE_COUNTS)


_MONITORED_PIPELINE_COUNTS = (
    "issuer_eligible_population",
    "input_ready_population",
    "dynamic_filter_eligible",
    "strategy_history_eligible",
    "model_input_eligible",
    "candidate_score_eligible",
    "candidate_limit_selected",
    "requested_candidates",
    "candidate_features",
    "candidate_quote_eligible",
    "security_master",
    "history",
    "full_scored",
)

_PIPELINE_STAGE_ORDER = (
    "input_readiness",
    "dynamic_filter",
    "board_cross_section",
    "strategy_history",
    "model_input",
    "candidate_score",
    "board_limit",
    "candidate_refresh",
    "input_coverage",
    "evidence_score",
    "model_cost_gate",
    "local_score",
    "deepseek_review",
    "fusion",
    "action_gate",
    "concentration",
)
_PIPELINE_STAGE_STATES = frozenset({"pending", "running", "completed", "degraded", "not_applicable"})

_PIPELINE_COUNT_LOCATIONS = {
    "issuer_eligible_population": ("input_readiness", "input_count"),
    "input_ready_population": ("input_readiness", "output_count"),
    "dynamic_filter_eligible": ("dynamic_filter", "output_count"),
    "strategy_history_eligible": ("strategy_history", "output_count"),
    "model_input_eligible": ("model_input", "output_count"),
    "candidate_score_eligible": ("candidate_score", "output_count"),
    "candidate_limit_selected": ("board_limit", "output_count"),
    "requested_candidates": ("candidate_refresh", "input_count"),
    "candidate_features": ("input_coverage", "candidate_features"),
    "candidate_quote_eligible": ("candidate_refresh", "output_count"),
    "security_master": ("input_coverage", "security_master"),
    "history": ("input_coverage", "history"),
    "filter_pass": ("board_cross_section", "filter_pass"),
    "filter_observe": ("board_cross_section", "filter_observe"),
    "filter_reject": ("board_cross_section", "filter_reject"),
    "full_scored": ("evidence_score", "output_count"),
    "review_eligible": ("deepseek_review", "input_count"),
    "observation_threshold_met_count": ("action_gate", "observation_threshold_met"),
    "executable_threshold_met_count": ("action_gate", "executable_threshold_met"),
    "action_executable": ("action_gate", "action_executable"),
    "action_observe": ("action_gate", "action_observe"),
    "action_unavailable": ("action_gate", "action_unavailable"),
    "selected_executable": ("concentration", "selected_executable"),
    "selected_observe": ("concentration", "selected_observe"),
}


@dataclass(frozen=True)
class CoverageSnapshot:
    candidate_count: int | None
    evaluated_count: int | None
    selected_count: int | None


@dataclass(frozen=True)
class ProjectionSnapshot:
    schema_version: str | None
    strategy: str | None
    status: str | None
    trade_date: str | None
    projection_version: str | None
    frozen: bool | None
    coverage: CoverageSnapshot
    item_count: int | None
    empty_reason: str | None
    maximum_final_score: float | None
    top_score_count: int | None
    highest_top_score: float | None
    pipeline: PipelineSnapshot | None
    degraded_reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class InputQualitySnapshot:
    status: str | None
    trade_date: str | None
    primary_blocker: str | None
    population_count: int | None
    history_required_sessions: int | None
    highest_final_score: float | None
    pipeline: PipelineSnapshot
    population_filter_reason_counts: Mapping[str, int]
    candidate_filter_reason_counts: Mapping[str, int]
    candidate_transient_reason_counts: Mapping[str, int]
    candidate_optional_reason_counts: Mapping[str, int]
    supply_reason_counts: Mapping[str, int]

    def __post_init__(self) -> None:
        for field_name in (
            "population_filter_reason_counts",
            "candidate_filter_reason_counts",
            "candidate_transient_reason_counts",
            "candidate_optional_reason_counts",
            "supply_reason_counts",
        ):
            object.__setattr__(self, field_name, MappingProxyType(dict(getattr(self, field_name))))


@dataclass(frozen=True)
class HistoryWarmupSnapshot:
    universe_rows: int | None
    covered_rows: int | None
    coverage_ratio: float | None
    planned_count: int | None
    completed_count: int | None
    failure_count: int | None
    inflight_count: int | None
    retry_deferred_count: int | None
    unique_failure_count: int | None
    timeout_count: int | None
    inflight_age_seconds: float | None
    batch_timeout_seconds: float | None
    last_source: str | None


@dataclass(frozen=True)
class CandidateQuoteAgeSnapshot:
    p50_seconds: float | None
    p95_seconds: float | None
    maximum_seconds: float | None
    sample_count: int | None


@dataclass(frozen=True)
class ModelIndustrySourceSnapshot:
    snapshot_rows: int | None
    error_count: int | None
    timeout_count: int | None
    last_error_code: str | None


@dataclass(frozen=True)
class ScoringHeadSnapshot:
    profile_id: str | None
    active: bool | None
    model_id: str | None
    model_hash: str | None
    request_count: int | None
    candidate_count: int | None
    predictor_batch_count: int | None
    cache_hit_count: int | None


@dataclass(frozen=True)
class ScoringProfileSnapshot:
    profile_id: str | None
    heads: Mapping[str, ScoringHeadSnapshot]

    def __post_init__(self) -> None:
        object.__setattr__(self, "heads", MappingProxyType(dict(self.heads)))


@dataclass(frozen=True)
class CompanyResearchSnapshot:
    state: str | None
    running_codes: int | None
    pending_codes: int | None
    completed_batches: int | None
    partial_batches: int | None
    failed_batches: int | None
    deferred_codes: int | None
    cooldown_codes: int | None
    retry_wait_codes: int | None
    next_retry_seconds: float | None
    gated_offer_codes: int | None
    short_circuited_batches: int | None
    short_circuited_codes: int | None
    tracked_code_gates: int | None
    evicted_code_gates: int | None
    batch_size: int | None
    batch_budget_seconds: float | None
    success_cooldown_seconds: float | None
    retry_delays_seconds: tuple[float, ...]
    trade_date: str | None
    tracked_strategies: int | None
    tracked_output_codes: int | None
    next_periodic_at: str | None
    intent_offer_count: int | None
    periodic_offer_count: int | None
    result_count: int | None
    rescore_result_count: int | None


@dataclass(frozen=True)
class WebRuntimeIssue:
    code: str | None
    strategy: str | None
    count: int | None


@dataclass(frozen=True)
class StatusSnapshot:
    schema_version: str | None
    release_decision_schema: str | None
    runtime_status: str | None
    runtime_started: bool
    runtime_version: str | None
    phase: str | None
    degraded_reasons: tuple[str, ...]
    event_sequence: int | None
    market_feature_rows: int | None
    candidate_quote_entries: int | None
    candidate_quote_source: str | None
    candidate_quote_age: CandidateQuoteAgeSnapshot
    model_industry_source: ModelIndustrySourceSnapshot
    history_warmup: HistoryWarmupSnapshot
    company_research: CompanyResearchSnapshot
    scoring_profile: ScoringProfileSnapshot
    recent_errors: tuple[WebRuntimeIssue, ...]
    strategies: Mapping[str, ProjectionSnapshot]
    input_quality: Mapping[str, InputQualitySnapshot]

    def __post_init__(self) -> None:
        object.__setattr__(self, "strategies", MappingProxyType(dict(self.strategies)))
        object.__setattr__(self, "input_quality", MappingProxyType(dict(self.input_quality)))


@dataclass(frozen=True)
class WebSample:
    sample_number: int
    collected_at: str
    status: StatusSnapshot | None
    decisions: Mapping[str, ProjectionSnapshot]
    fetch_issues: tuple[FetchIssue, ...] = ()

    def __post_init__(self) -> None:
        if self.sample_number < 1:
            raise ValueError("sample number must be positive")
        object.__setattr__(self, "decisions", MappingProxyType(dict(self.decisions)))


def parse_web_sample(
    sample_number: int,
    collected_at: str,
    *,
    status_payload: Mapping[str, object] | None,
    decision_payloads: Mapping[str, Mapping[str, object]],
    fetch_issues: tuple[FetchIssue, ...] = (),
) -> WebSample:
    """Parse external JSON projections into the immutable diagnostic model."""

    return WebSample(
        sample_number,
        collected_at,
        _parse_status(status_payload) if status_payload is not None else None,
        {strategy: _parse_projection(payload, include_items=True) for strategy, payload in decision_payloads.items()},
        fetch_issues,
    )


def _parse_status(payload: Mapping[str, object]) -> StatusSnapshot:
    release = _mapping(payload.get("release"))
    market = _mapping(payload.get("market_data"))
    events = _mapping(payload.get("events"))
    scheduler = _mapping(payload.get("scheduler"))
    company_research = _mapping(payload.get("company_research"))
    scoring_profile = _mapping(payload.get("scoring_profile"))
    candidate_quote_age = _mapping(market.get("candidate_quote_age"))
    model_industry_source = _mapping(_mapping(market.get("sources")).get("baostock_industry"))
    strategies = {
        strategy: _parse_projection(value, include_items=False)
        for strategy, raw in _mapping(payload.get("strategies")).items()
        if (value := _mapping_or_none(raw)) is not None
    }
    input_quality = {
        strategy: _parse_input_quality(value)
        for strategy, raw in _mapping(scheduler.get("input_quality")).items()
        if (value := _mapping_or_none(raw)) is not None
    }
    return StatusSnapshot(
        schema_version=_text(payload.get("schema_version")),
        release_decision_schema=_text(release.get("decision_view_schema")),
        runtime_status=_text(payload.get("status")),
        runtime_started=payload.get("runtime_started") is True,
        runtime_version=_text(payload.get("runtime_version")),
        phase=_text(payload.get("phase")),
        degraded_reasons=_text_tuple(payload.get("degraded_reasons"), limit=32),
        event_sequence=_nonnegative_int(events.get("sequence")),
        market_feature_rows=_nonnegative_int(market.get("market_feature_rows")),
        candidate_quote_entries=_nonnegative_int(market.get("candidate_quote_cache_entries")),
        candidate_quote_source=_text(market.get("candidate_quote_latest_source")),
        candidate_quote_age=CandidateQuoteAgeSnapshot(
            p50_seconds=_nonnegative_number(candidate_quote_age.get("p50_seconds")),
            p95_seconds=_nonnegative_number(candidate_quote_age.get("p95_seconds")),
            maximum_seconds=_nonnegative_number(candidate_quote_age.get("maximum_seconds")),
            sample_count=_nonnegative_int(candidate_quote_age.get("sample_count")),
        ),
        model_industry_source=ModelIndustrySourceSnapshot(
            snapshot_rows=_nonnegative_int(model_industry_source.get("snapshot_rows")),
            error_count=_nonnegative_int(model_industry_source.get("error_count")),
            timeout_count=_nonnegative_int(model_industry_source.get("timeout_count")),
            last_error_code=_text(model_industry_source.get("last_error_code")),
        ),
        history_warmup=HistoryWarmupSnapshot(
            universe_rows=_nonnegative_int(market.get("history_universe_rows")),
            covered_rows=_nonnegative_int(market.get("history_covered_rows")),
            coverage_ratio=_nonnegative_number(market.get("history_coverage_ratio")),
            planned_count=_nonnegative_int(market.get("history_warmup_planned_count")),
            completed_count=_nonnegative_int(market.get("history_warmup_completed_count")),
            failure_count=_nonnegative_int(market.get("history_warmup_failure_count")),
            inflight_count=_nonnegative_int(market.get("history_warmup_inflight_count")),
            retry_deferred_count=_nonnegative_int(market.get("history_warmup_retry_deferred_count")),
            unique_failure_count=_nonnegative_int(market.get("history_warmup_unique_failure_count")),
            timeout_count=_nonnegative_int(market.get("history_warmup_timeout_count")),
            inflight_age_seconds=_nonnegative_number(market.get("history_warmup_inflight_age_seconds")),
            batch_timeout_seconds=_nonnegative_number(market.get("history_warmup_batch_timeout_seconds")),
            last_source=_text(market.get("history_warmup_last_source")),
        ),
        company_research=CompanyResearchSnapshot(
            state=_text(company_research.get("state")),
            running_codes=_nonnegative_int(company_research.get("running_codes")),
            pending_codes=_nonnegative_int(company_research.get("pending_codes")),
            completed_batches=_nonnegative_int(company_research.get("completed_batches")),
            partial_batches=_nonnegative_int(company_research.get("partial_batches")),
            failed_batches=_nonnegative_int(company_research.get("failed_batches")),
            deferred_codes=_nonnegative_int(company_research.get("deferred_codes")),
            cooldown_codes=_nonnegative_int(company_research.get("cooldown_codes")),
            retry_wait_codes=_nonnegative_int(company_research.get("retry_wait_codes")),
            next_retry_seconds=_nonnegative_number(company_research.get("next_retry_seconds")),
            gated_offer_codes=_nonnegative_int(company_research.get("gated_offer_codes")),
            short_circuited_batches=_nonnegative_int(company_research.get("short_circuited_batches")),
            short_circuited_codes=_nonnegative_int(company_research.get("short_circuited_codes")),
            tracked_code_gates=_nonnegative_int(company_research.get("tracked_code_gates")),
            evicted_code_gates=_nonnegative_int(company_research.get("evicted_code_gates")),
            batch_size=_nonnegative_int(company_research.get("batch_size")),
            batch_budget_seconds=_nonnegative_number(company_research.get("batch_budget_seconds")),
            success_cooldown_seconds=_nonnegative_number(company_research.get("success_cooldown_seconds")),
            retry_delays_seconds=_number_tuple(company_research.get("retry_delays_seconds"), limit=8),
            trade_date=_text(company_research.get("trade_date")),
            tracked_strategies=_nonnegative_int(company_research.get("tracked_strategies")),
            tracked_output_codes=_nonnegative_int(company_research.get("tracked_output_codes")),
            next_periodic_at=_text(company_research.get("next_periodic_at")),
            intent_offer_count=_nonnegative_int(company_research.get("intent_offer_count")),
            periodic_offer_count=_nonnegative_int(company_research.get("periodic_offer_count")),
            result_count=_nonnegative_int(company_research.get("result_count")),
            rescore_result_count=_nonnegative_int(company_research.get("rescore_result_count")),
        ),
        scoring_profile=_parse_scoring_profile(scoring_profile),
        recent_errors=_parse_runtime_issues(payload.get("recent_errors")),
        strategies=strategies,
        input_quality=input_quality,
    )


def _parse_projection(payload: Mapping[str, object], *, include_items: bool) -> ProjectionSnapshot:
    coverage = _mapping(payload.get("coverage"))
    diagnostics = _mapping(payload.get("selection_diagnostics"))
    items = payload.get("items")
    frozen = payload.get("frozen")
    top_score_count, highest_top_score = _top_score_summary(payload.get("top_scores"))
    pipeline_payload = _mapping_or_none(payload.get("pipeline"))
    return ProjectionSnapshot(
        schema_version=_text(payload.get("schema_version")),
        strategy=_text(payload.get("strategy")),
        status=_text(payload.get("status")),
        trade_date=_text(payload.get("trade_date")),
        projection_version=_text(payload.get("projection_version")),
        frozen=frozen if isinstance(frozen, bool) else None,
        coverage=CoverageSnapshot(
            candidate_count=_nonnegative_int(coverage.get("candidate_count")),
            evaluated_count=_nonnegative_int(coverage.get("evaluated_count")),
            selected_count=_nonnegative_int(coverage.get("selected_count")),
        ),
        item_count=len(items) if include_items and isinstance(items, list) else None,
        empty_reason=_text(diagnostics.get("empty_reason")),
        maximum_final_score=_nonnegative_number(diagnostics.get("maximum_final_score")),
        top_score_count=top_score_count,
        highest_top_score=highest_top_score,
        pipeline=_parse_pipeline(pipeline_payload) if pipeline_payload is not None else None,
        degraded_reasons=_text_tuple(payload.get("degraded_reasons"), limit=32),
    )


def _parse_input_quality(payload: Mapping[str, object]) -> InputQualitySnapshot:
    summary = _mapping(payload.get("summary"))
    return InputQualitySnapshot(
        status=_text(payload.get("status")),
        trade_date=_text(summary.get("trade_date")),
        primary_blocker=_text(payload.get("primary_blocker")),
        population_count=_nonnegative_int(payload.get("population_count")),
        history_required_sessions=_nonnegative_int(payload.get("history_required_sessions")),
        highest_final_score=_nonnegative_number(summary.get("highest_final_score")),
        pipeline=_parse_pipeline(_mapping(payload.get("pipeline"))),
        population_filter_reason_counts=_parse_reason_counts(payload.get("population_filter_reason_counts")),
        candidate_filter_reason_counts=_parse_reason_counts(payload.get("candidate_filter_reason_counts")),
        candidate_transient_reason_counts=_parse_reason_counts(payload.get("candidate_transient_reason_counts")),
        candidate_optional_reason_counts=_parse_reason_counts(payload.get("candidate_optional_reason_counts")),
        supply_reason_counts=_parse_reason_counts(payload.get("supply_reason_counts")),
    )


def _parse_scoring_profile(payload: Mapping[str, object]) -> ScoringProfileSnapshot:
    heads = {
        strategy: _parse_scoring_head(value)
        for strategy, raw in _mapping(payload.get("heads")).items()
        if (value := _mapping_or_none(raw)) is not None
    }
    return ScoringProfileSnapshot(_text(payload.get("profile_id")), heads)


def _parse_scoring_head(payload: Mapping[str, object]) -> ScoringHeadSnapshot:
    computation = _mapping(payload.get("computation"))
    active = payload.get("active")
    return ScoringHeadSnapshot(
        profile_id=_text(payload.get("profile_id")),
        active=active if isinstance(active, bool) else None,
        model_id=_text(payload.get("model_id")),
        model_hash=_text(payload.get("model_hash")),
        request_count=_nonnegative_int(computation.get("request_count")),
        candidate_count=_nonnegative_int(computation.get("candidate_count")),
        predictor_batch_count=_nonnegative_int(computation.get("predictor_batch_count")),
        cache_hit_count=_nonnegative_int(computation.get("cache_hit_count")),
    )


def _top_score_summary(value: object) -> tuple[int | None, float | None]:
    if not isinstance(value, (list, tuple)):
        return None, None
    scores = tuple(
        score
        for raw in value[:3]
        if (score := _nonnegative_number(_mapping(_mapping(raw).get("scores")).get("final"))) is not None
    )
    return len(value), max(scores) if scores else None


def _parse_pipeline(payload: Mapping[str, object]) -> PipelineSnapshot:
    raw_stages = payload.get("stages")
    if not isinstance(raw_stages, (list, tuple)):
        return PipelineSnapshot(_text(payload.get("current_stage")), (), ("stages",))
    stages: list[PipelineStageSnapshot] = []
    invalid_fields: list[str] = []
    for index, raw in enumerate(raw_stages):
        stage_payload = _mapping_or_none(raw)
        if stage_payload is None:
            invalid_fields.append(f"stages[{index}]")
            continue
        stage = _parse_pipeline_stage(stage_payload, index)
        stages.append(stage)
        invalid_fields.extend(stage.invalid_fields)
    keys = tuple(stage.key for stage in stages)
    current_stage = _text(payload.get("current_stage"))
    if keys != _PIPELINE_STAGE_ORDER:
        invalid_fields.append("stages.order")
    if current_stage not in keys:
        invalid_fields.append("current_stage")
    return PipelineSnapshot(
        current_stage,
        tuple(stages),
        tuple(sorted(set(invalid_fields))),
    )


def _parse_pipeline_stage(payload: Mapping[str, object], index: int) -> PipelineStageSnapshot:
    key = _text(payload.get("key"))
    state = _text(payload.get("state"))
    invalid: list[str] = []
    if key is None:
        invalid.append(f"stages[{index}].key")
    if state is None:
        invalid.append(f"stages[{index}].state")
    elif state not in _PIPELINE_STAGE_STATES:
        invalid.append(f"stages[{index}].state")
    input_count = _optional_count(payload, "input_count", invalid, f"stages[{index}]")
    output_count = _optional_count(payload, "output_count", invalid, f"stages[{index}]")
    facets: list[PipelineFacetSnapshot] = []
    raw_facets = payload.get("facets", ())
    if not isinstance(raw_facets, (list, tuple)):
        invalid.append(f"stages[{index}].facets")
    else:
        for facet_index, raw in enumerate(raw_facets):
            facet_payload = _mapping_or_none(raw)
            if facet_payload is None:
                invalid.append(f"stages[{index}].facets[{facet_index}]")
                continue
            prefix = f"stages[{index}].facets[{facet_index}]"
            facet_key = _text(facet_payload.get("key"))
            if facet_key is None:
                invalid.append(f"{prefix}.key")
            count = _required_count(facet_payload, "count", invalid, prefix)
            total = _optional_count(facet_payload, "total", invalid, prefix)
            if count is not None and total is not None and count > total:
                invalid.append(f"{prefix}.count")
            facets.append(PipelineFacetSnapshot(facet_key, count, total))
    if input_count is not None and output_count is not None and output_count > input_count:
        invalid.append(f"stages[{index}].output_count")
    facet_keys = tuple(facet.key for facet in facets)
    if len(facet_keys) != len(set(facet_keys)):
        invalid.append(f"stages[{index}].facets")
    return PipelineStageSnapshot(key, state, input_count, output_count, tuple(facets), tuple(invalid))


def _optional_count(
    payload: Mapping[str, object],
    key: str,
    invalid: list[str],
    prefix: str,
) -> int | None:
    raw = payload.get(key)
    value = _nonnegative_int(raw)
    if raw is not None and value is None:
        invalid.append(f"{prefix}.{key}")
    return value


def _required_count(
    payload: Mapping[str, object],
    key: str,
    invalid: list[str],
    prefix: str,
) -> int | None:
    value = _optional_count(payload, key, invalid, prefix)
    if payload.get(key) is None:
        invalid.append(f"{prefix}.{key}")
    return value


def _parse_runtime_issues(value: object) -> tuple[WebRuntimeIssue, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    issues: list[WebRuntimeIssue] = []
    for raw in value[:32]:
        payload = _mapping_or_none(raw)
        if payload is None:
            continue
        issues.append(
            WebRuntimeIssue(
                code=_text(payload.get("code")),
                strategy=_text(payload.get("strategy")),
                count=_nonnegative_int(payload.get("count")),
            )
        )
    return tuple(issues)


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): item for key, item in value.items() if isinstance(key, str)}


def _mapping_or_none(value: object) -> Mapping[str, object] | None:
    return _mapping(value) if isinstance(value, Mapping) else None


def _parse_reason_counts(value: object) -> Mapping[str, int]:
    counts = {key: count for key, raw in _mapping(value).items() if (count := _nonnegative_int(raw)) is not None}
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:_MAX_REASON_COUNTS]
    return MappingProxyType(dict(ordered))


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _text_tuple(value: object, *, limit: int) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(item for item in value[:limit] if isinstance(item, str) and item)


def _number_tuple(value: object, *, limit: int) -> tuple[float, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(number for item in value[:limit] if (number := _nonnegative_number(item)) is not None)


def _nonnegative_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _nonnegative_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value) or value < 0:
        return None
    return float(value)


__all__ = [
    "FetchIssue",
    "InputQualitySnapshot",
    "PipelineFacetSnapshot",
    "PipelineSnapshot",
    "PipelineStageSnapshot",
    "ProjectionSnapshot",
    "ScoringHeadSnapshot",
    "ScoringProfileSnapshot",
    "WebRuntimeIssue",
    "WebSample",
    "parse_web_sample",
]
