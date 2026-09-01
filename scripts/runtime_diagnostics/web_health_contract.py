"""Typed parsing boundary for sanitized V2 Web-health samples."""

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
class FunnelSnapshot:
    requested_candidates: int | None
    candidate_features: int | None
    security_master: int | None
    history: int | None
    filter_pass: int | None
    filter_observe: int | None
    filter_reject: int | None
    full_scored: int | None
    review_eligible: int | None
    action_executable: int | None
    action_observe: int | None
    action_unavailable: int | None
    selected_executable: int | None
    selected_observe: int | None
    invalid_fields: tuple[str, ...] = ()

    def monitored_counts(self) -> tuple[tuple[str, int | None], ...]:
        return (
            ("requested_candidates", self.requested_candidates),
            ("candidate_features", self.candidate_features),
            ("security_master", self.security_master),
            ("history", self.history),
            ("full_scored", self.full_scored),
        )


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
    degraded_reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class InputQualitySnapshot:
    status: str | None
    trade_date: str | None
    primary_blocker: str | None
    history_required_sessions: int | None
    funnel: FunnelSnapshot
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
class StatusSnapshot:
    schema_version: str | None
    release_decision_schema: str | None
    web_asset_revision: str | None
    runtime_status: str | None
    runtime_started: bool
    runtime_version: str | None
    phase: str | None
    event_sequence: int | None
    market_feature_rows: int | None
    candidate_quote_entries: int | None
    candidate_quote_source: str | None
    history_warmup: HistoryWarmupSnapshot
    company_research: CompanyResearchSnapshot
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
        web_asset_revision=_text(release.get("web_asset_revision")),
        runtime_status=_text(payload.get("status")),
        runtime_started=payload.get("runtime_started") is True,
        runtime_version=_text(payload.get("runtime_version")),
        phase=_text(payload.get("phase")),
        event_sequence=_nonnegative_int(events.get("sequence")),
        market_feature_rows=_nonnegative_int(market.get("market_feature_rows")),
        candidate_quote_entries=_nonnegative_int(market.get("candidate_quote_cache_entries")),
        candidate_quote_source=_text(market.get("candidate_quote_latest_source")),
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
        strategies=strategies,
        input_quality=input_quality,
    )


def _parse_projection(payload: Mapping[str, object], *, include_items: bool) -> ProjectionSnapshot:
    coverage = _mapping(payload.get("coverage"))
    diagnostics = _mapping(payload.get("selection_diagnostics"))
    items = payload.get("items")
    frozen = payload.get("frozen")
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
        degraded_reasons=_text_tuple(payload.get("degraded_reasons"), limit=32),
    )


def _parse_input_quality(payload: Mapping[str, object]) -> InputQualitySnapshot:
    return InputQualitySnapshot(
        status=_text(payload.get("status")),
        trade_date=_text(_mapping(payload.get("summary")).get("trade_date")),
        primary_blocker=_text(payload.get("primary_blocker")),
        history_required_sessions=_nonnegative_int(payload.get("history_required_sessions")),
        funnel=_parse_funnel(_mapping(payload.get("supply_funnel"))),
        population_filter_reason_counts=_parse_reason_counts(payload.get("population_filter_reason_counts")),
        candidate_filter_reason_counts=_parse_reason_counts(payload.get("candidate_filter_reason_counts")),
        candidate_transient_reason_counts=_parse_reason_counts(payload.get("candidate_transient_reason_counts")),
        candidate_optional_reason_counts=_parse_reason_counts(payload.get("candidate_optional_reason_counts")),
        supply_reason_counts=_parse_reason_counts(payload.get("supply_reason_counts")),
    )


def _parse_funnel(payload: Mapping[str, object]) -> FunnelSnapshot:
    field_names = (
        "requested_candidates",
        "candidate_features",
        "security_master",
        "history",
        "filter_pass",
        "filter_observe",
        "filter_reject",
        "full_scored",
        "review_eligible",
        "action_executable",
        "action_observe",
        "action_unavailable",
        "selected_executable",
        "selected_observe",
    )
    values = {name: _nonnegative_int(payload.get(name)) for name in field_names}
    return FunnelSnapshot(
        requested_candidates=values["requested_candidates"],
        candidate_features=values["candidate_features"],
        security_master=values["security_master"],
        history=values["history"],
        filter_pass=values["filter_pass"],
        filter_observe=values["filter_observe"],
        filter_reject=values["filter_reject"],
        full_scored=values["full_scored"],
        review_eligible=values["review_eligible"],
        action_executable=values["action_executable"],
        action_observe=values["action_observe"],
        action_unavailable=values["action_unavailable"],
        selected_executable=values["selected_executable"],
        selected_observe=values["selected_observe"],
        invalid_fields=tuple(name for name in field_names if values[name] is None),
    )


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
    "FunnelSnapshot",
    "InputQualitySnapshot",
    "ProjectionSnapshot",
    "WebSample",
    "parse_web_sample",
]
