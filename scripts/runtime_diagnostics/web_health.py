#!/usr/bin/env python3
"""Sample live V2 Web APIs and detect recommendation-funnel state anomalies."""

from __future__ import annotations

import argparse
import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Literal
from zoneinfo import ZoneInfo

from .common import emit_report

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_STRATEGIES = ("today", "tomorrow", "d25")
_SCORING_PHASES = frozenset(
    {
        "today_observe",
        "today_main",
        "today_late",
        "afternoon",
        "final_review",
        "final_quote",
    }
)
_TODAY_SCORING_PHASES = frozenset({"today_observe", "today_main", "today_late"})
_MONITORED_FUNNEL_FIELDS = (
    "requested_candidates",
    "candidate_features",
    "security_master",
    "history",
    "full_scored",
)
_MAX_RESPONSE_BYTES = 1_048_576
_EvidenceValue = str | int | float | bool | None


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


@dataclass(frozen=True)
class InputQualitySnapshot:
    status: str | None
    trade_date: str | None
    primary_blocker: str | None
    funnel: FunnelSnapshot


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


@dataclass(frozen=True)
class Finding:
    severity: Literal["warning", "error"]
    code: str
    strategy: str | None
    first_sample: int
    last_sample: int
    occurrences: int
    message: str
    evidence: Mapping[str, _EvidenceValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence", MappingProxyType(dict(self.evidence)))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:5000", help="running trader-server base URL")
    parser.add_argument("--samples", type=int, default=6, help="number of API sample rounds (default: 6)")
    parser.add_argument(
        "--interval-seconds",
        type=float,
        default=5.0,
        help="wall-clock delay between samples (default: 5)",
    )
    parser.add_argument("--timeout-seconds", type=float, default=3.0, help="timeout per HTTP request")
    parser.add_argument(
        "--consecutive-zero-threshold",
        type=int,
        default=3,
        help="consecutive eligible zero samples required for a persistent-zero finding",
    )
    parser.add_argument(
        "--strategy",
        action="append",
        choices=_STRATEGIES,
        dest="strategies",
        help="short strategy to inspect; repeat to select multiple (default: all)",
    )
    return parser


def analyze_samples(
    samples: Sequence[WebSample],
    *,
    strategies: tuple[str, ...] = _STRATEGIES,
    consecutive_zero_threshold: int = 3,
) -> tuple[Finding, ...]:
    if consecutive_zero_threshold < 1:
        raise ValueError("consecutive zero threshold must be positive")
    findings: list[Finding] = []
    for sample in samples:
        findings.extend(_sample_findings(sample, strategies))
    findings.extend(_regression_findings(samples, strategies))
    findings.extend(_persistent_zero_findings(samples, strategies, consecutive_zero_threshold))
    return _coalesce_findings(findings)


def _sample_findings(sample: WebSample, strategies: tuple[str, ...]) -> list[Finding]:
    findings = [
        _finding(
            "error",
            "http_fetch_failed",
            sample,
            None,
            "Web diagnostic endpoint could not be read",
            {"endpoint": issue.endpoint, "error_code": issue.error_code},
        )
        for issue in sample.fetch_issues
    ]
    status = sample.status
    if status is None:
        return findings
    if status.schema_version != "v2_status_v4":
        findings.append(
            _finding("error", "status_schema_mismatch", sample, None, "status schema is missing or unsupported")
        )
    if not status.runtime_started or status.runtime_status != "running":
        findings.append(_finding("error", "runtime_not_running", sample, None, "runtime is not reported as running"))
    if not status.release_decision_schema or not status.web_asset_revision:
        findings.append(
            _finding("error", "release_identity_missing", sample, None, "status release identity is incomplete")
        )
    if status.phase in _SCORING_PHASES and (
        status.market_feature_rows is None or status.candidate_quote_entries is None
    ):
        findings.append(
            _finding(
                "error",
                "market_telemetry_missing",
                sample,
                None,
                "status lacks recommendation market-cache telemetry during a scoring phase",
            )
        )
    for strategy in strategies:
        strategy_status = status.strategies.get(strategy)
        decision = sample.decisions.get(strategy)
        if decision is not None:
            findings.extend(
                _decision_contract_findings(
                    sample,
                    strategy,
                    strategy_status,
                    decision,
                    status.release_decision_schema,
                )
            )
        quality = status.input_quality.get(strategy)
        if quality is not None:
            if quality.status is None or quality.trade_date is None or quality.primary_blocker is None:
                findings.append(
                    _finding(
                        "error",
                        "input_quality_shape_invalid",
                        sample,
                        strategy,
                        "input quality lacks status, trade date, or primary blocker",
                    )
                )
            findings.extend(_funnel_consistency_findings(sample, strategy, quality.funnel))
    return findings


def _decision_contract_findings(
    sample: WebSample,
    strategy: str,
    strategy_status: ProjectionSnapshot | None,
    decision: ProjectionSnapshot,
    release_schema: str | None,
) -> list[Finding]:
    findings: list[Finding] = []
    comparisons = (
        ()
        if strategy_status is None
        else (
            ("status", "status", strategy_status.status, decision.status),
            ("trade_date", "trade_date", strategy_status.trade_date, decision.trade_date),
            (
                "projection",
                "projection_version",
                strategy_status.projection_version,
                decision.projection_version,
            ),
            (
                "candidate_count",
                "candidate_count",
                strategy_status.coverage.candidate_count,
                decision.coverage.candidate_count,
            ),
            (
                "evaluated_count",
                "evaluated_count",
                strategy_status.coverage.evaluated_count,
                decision.coverage.evaluated_count,
            ),
            (
                "selected_count",
                "selected_count",
                strategy_status.coverage.selected_count,
                decision.coverage.selected_count,
            ),
        )
    )
    for code_field, evidence_field, status_value, decision_value in comparisons:
        if status_value != decision_value:
            findings.append(
                _finding(
                    "warning" if code_field == "projection" else "error",
                    f"status_current_{code_field}_mismatch",
                    sample,
                    strategy,
                    "status and current decision identities disagree",
                    {"field": evidence_field},
                )
            )
    if strategy_status is None:
        findings.append(
            _finding(
                "error",
                "status_strategy_missing",
                sample,
                strategy,
                "status lacks the requested strategy projection",
            )
        )
    if decision.strategy != strategy:
        findings.append(
            _finding("error", "current_strategy_mismatch", sample, strategy, "current decision strategy is incorrect")
        )
    if release_schema and decision.schema_version != release_schema:
        findings.append(
            _finding(
                "error",
                "release_contract_mismatch",
                sample,
                strategy,
                "current decision schema does not match the loaded release identity",
            )
        )
    selected_count = decision.coverage.selected_count
    if selected_count is None or decision.item_count is None:
        findings.append(
            _finding(
                "error",
                "current_projection_shape_invalid",
                sample,
                strategy,
                "current decision coverage or item collection is invalid",
            )
        )
    if selected_count is not None and decision.item_count is not None and selected_count != decision.item_count:
        findings.append(
            _finding(
                "error",
                "coverage_item_count_mismatch",
                sample,
                strategy,
                "current decision selected count does not match its item count",
                {"selected_count": selected_count, "item_count": decision.item_count},
            )
        )
    if decision.status == "ready" and selected_count == 0 and decision.frozen is not True and not decision.empty_reason:
        findings.append(
            _finding(
                "error",
                "legal_empty_diagnostics_missing",
                sample,
                strategy,
                "ready decision with zero selected items lacks a legal-empty explanation",
            )
        )
    return findings


def _funnel_consistency_findings(
    sample: WebSample,
    strategy: str,
    funnel: FunnelSnapshot,
) -> list[Finding]:
    if funnel.invalid_fields:
        return [
            _finding(
                "error",
                "funnel_count_invalid",
                sample,
                strategy,
                "recommendation funnel contains non-integer or negative counts",
                {"fields": ",".join(funnel.invalid_fields)},
            )
        ]
    requested = funnel.requested_candidates or 0
    candidates = funnel.candidate_features or 0
    full_scored = funnel.full_scored or 0
    selected = (funnel.selected_executable or 0) + (funnel.selected_observe or 0)
    filtered = (funnel.filter_pass or 0) + (funnel.filter_observe or 0) + (funnel.filter_reject or 0)
    inconsistent = {
        "candidate_features": candidates > requested,
        "security_master": (funnel.security_master or 0) > requested,
        "history": (funnel.history or 0) > requested,
        "filter_total": filtered > requested,
        "full_scored": full_scored > candidates,
        "selected_total": selected > full_scored,
    }
    fields = tuple(name for name, failed in inconsistent.items() if failed)
    if not fields:
        return []
    return [
        _finding(
            "error",
            "funnel_count_inconsistent",
            sample,
            strategy,
            "recommendation funnel stages violate their count bounds",
            {"fields": ",".join(fields)},
        )
    ]


def _regression_findings(samples: Sequence[WebSample], strategies: tuple[str, ...]) -> list[Finding]:
    findings: list[Finding] = []
    for previous, current in zip(samples, samples[1:], strict=False):
        if _runtime_restarted(previous, current):
            findings.append(
                _finding(
                    "warning",
                    "runtime_restart_observed",
                    current,
                    None,
                    "event sequence regressed; cross-sample funnel comparisons were reset",
                )
            )
            continue
        previous_status = previous.status
        current_status = current.status
        if previous_status is not None and current_status is not None:
            before = previous_status.history_warmup.failure_count
            after = current_status.history_warmup.failure_count
            if before is not None and after is not None and after > before:
                findings.append(
                    _finding(
                        "warning",
                        "history_warmup_failures_increased",
                        current,
                        None,
                        "history warmup failure counter increased while sampling",
                        {"previous": before, "current": after, "delta": after - before},
                    )
                )
            before_timeout = previous_status.history_warmup.timeout_count
            after_timeout = current_status.history_warmup.timeout_count
            if before_timeout is not None and after_timeout is not None and after_timeout > before_timeout:
                findings.append(
                    _finding(
                        "error",
                        "history_warmup_timeouts_increased",
                        current,
                        None,
                        "history warmup timeout counter increased while sampling",
                        {
                            "previous": before_timeout,
                            "current": after_timeout,
                            "delta": after_timeout - before_timeout,
                        },
                    )
                )
        for strategy in strategies:
            previous_quality = _strategy_quality(previous, strategy)
            current_quality = _strategy_quality(current, strategy)
            if (
                previous_quality is not None
                and current_quality is None
                and _strategy_expected_to_score(current, strategy)
            ):
                findings.append(
                    _finding(
                        "error",
                        "input_quality_disappeared",
                        current,
                        strategy,
                        "strategy input quality disappeared without a runtime restart",
                    )
                )
                continue
            if current_quality is None or previous_quality is None:
                continue
            if not _strategy_expected_to_score(current, strategy):
                continue
            if previous_quality.trade_date != current_quality.trade_date:
                continue
            previous_counts = dict(previous_quality.funnel.monitored_counts())
            for name, after in current_quality.funnel.monitored_counts():
                before = previous_counts[name]
                if before is not None and before > 0 and after == 0:
                    findings.append(
                        _finding(
                            "error",
                            "funnel_regressed_to_zero",
                            current,
                            strategy,
                            "a populated recommendation-funnel stage regressed to zero",
                            {"field": name, "previous": before},
                        )
                    )
    return findings


def _persistent_zero_findings(
    samples: Sequence[WebSample],
    strategies: tuple[str, ...],
    threshold: int,
) -> list[Finding]:
    findings: list[Finding] = []
    for strategy in strategies:
        missing_quality = [
            sample
            for sample in samples
            if _strategy_expected_to_score(sample, strategy)
            and _candidate_quote_entries(sample) > 0
            and _strategy_quality(sample, strategy) is None
        ]
        run = _longest_consecutive_run(missing_quality)
        if len(run) >= threshold:
            findings.append(
                _run_finding(
                    "input_quality_persistently_missing",
                    strategy,
                    run,
                    "strategy input quality stayed missing while candidate quotes were available",
                )
            )
        for field_name in _MONITORED_FUNNEL_FIELDS:
            zero_samples = [sample for sample in samples if _eligible_zero(sample, strategy, field_name)]
            run = _longest_consecutive_run(zero_samples)
            if len(run) < threshold:
                continue
            findings.append(
                _run_finding(
                    f"funnel_{field_name}_persistently_zero",
                    strategy,
                    run,
                    "recommendation-funnel stage stayed zero despite populated upstream inputs",
                    {"field": field_name},
                )
            )
    return findings


def _eligible_zero(sample: WebSample, strategy: str, field_name: str) -> bool:
    quality = _strategy_quality(sample, strategy)
    if not _strategy_expected_to_score(sample, strategy) or quality is None:
        return False
    funnel = quality.funnel
    counts = dict(funnel.monitored_counts())
    if counts[field_name] != 0 or (field_name == "full_scored" and quality.status == "business_empty"):
        return False
    requested = funnel.requested_candidates or 0
    candidate_features = funnel.candidate_features or 0
    if field_name == "requested_candidates":
        return _market_feature_rows(sample) > 0
    if field_name == "candidate_features":
        return requested > 0 and _candidate_quote_entries(sample) > 0
    return candidate_features > 0


def _strategy_expected_to_score(sample: WebSample, strategy: str) -> bool:
    status = sample.status
    if status is None or not status.runtime_started:
        return False
    phase = status.phase
    if phase not in _SCORING_PHASES:
        return False
    return strategy != "today" or phase in _TODAY_SCORING_PHASES


def _strategy_quality(sample: WebSample, strategy: str) -> InputQualitySnapshot | None:
    return sample.status.input_quality.get(strategy) if sample.status is not None else None


def _candidate_quote_entries(sample: WebSample) -> int:
    if sample.status is None:
        return 0
    return sample.status.candidate_quote_entries or 0


def _market_feature_rows(sample: WebSample) -> int:
    if sample.status is None:
        return 0
    return sample.status.market_feature_rows or 0


def _runtime_restarted(previous: WebSample, current: WebSample) -> bool:
    previous_sequence = previous.status.event_sequence if previous.status is not None else None
    current_sequence = current.status.event_sequence if current.status is not None else None
    return previous_sequence is not None and current_sequence is not None and current_sequence < previous_sequence


def _longest_consecutive_run(samples: Sequence[WebSample]) -> tuple[WebSample, ...]:
    best: tuple[WebSample, ...] = ()
    current: list[WebSample] = []
    for sample in samples:
        if current and (
            sample.sample_number != current[-1].sample_number + 1 or _runtime_restarted(current[-1], sample)
        ):
            if len(current) > len(best):
                best = tuple(current)
            current = []
        current.append(sample)
    if len(current) > len(best):
        best = tuple(current)
    return best


def _finding(
    severity: Literal["warning", "error"],
    code: str,
    sample: WebSample,
    strategy: str | None,
    message: str,
    evidence: Mapping[str, _EvidenceValue] | None = None,
) -> Finding:
    return Finding(severity, code, strategy, sample.sample_number, sample.sample_number, 1, message, evidence or {})


def _run_finding(
    code: str,
    strategy: str,
    run: Sequence[WebSample],
    message: str,
    evidence: Mapping[str, _EvidenceValue] | None = None,
) -> Finding:
    return Finding(
        "error",
        code,
        strategy,
        run[0].sample_number,
        run[-1].sample_number,
        len(run),
        message,
        evidence or {},
    )


def _coalesce_findings(findings: Sequence[Finding]) -> tuple[Finding, ...]:
    grouped: dict[tuple[str, str | None, str], Finding] = {}
    for finding in findings:
        discriminator = _text(finding.evidence.get("field")) or _text(finding.evidence.get("endpoint")) or ""
        key = finding.code, finding.strategy, discriminator
        existing = grouped.get(key)
        if existing is None:
            grouped[key] = finding
            continue
        grouped[key] = Finding(
            "error" if "error" in {existing.severity, finding.severity} else "warning",
            finding.code,
            finding.strategy,
            min(existing.first_sample, finding.first_sample),
            max(existing.last_sample, finding.last_sample),
            existing.occurrences + finding.occurrences,
            finding.message,
            finding.evidence,
        )
    return tuple(
        sorted(
            grouped.values(),
            key=lambda item: (
                0 if item.severity == "error" else 1,
                item.strategy or "",
                item.code,
                item.first_sample,
            ),
        )
    )


def collect_samples(
    base_url: str,
    *,
    strategies: tuple[str, ...],
    sample_count: int,
    interval_seconds: float,
    timeout_seconds: float,
) -> tuple[WebSample, ...]:
    samples: list[WebSample] = []
    for sample_number in range(1, sample_count + 1):
        samples.append(_collect_sample(base_url, strategies, sample_number, timeout_seconds))
        if sample_number < sample_count:
            time.sleep(interval_seconds)
    return tuple(samples)


def _collect_sample(
    base_url: str,
    strategies: tuple[str, ...],
    sample_number: int,
    timeout_seconds: float,
) -> WebSample:
    status_payload: Mapping[str, object] | None = None
    decision_payloads: dict[str, Mapping[str, object]] = {}
    issues: list[FetchIssue] = []
    try:
        status_payload = _fetch_mapping(f"{base_url}/api/v2/status", timeout_seconds)
    except FetchError as exc:
        issues.append(FetchIssue("status", exc.error_code))
    for strategy in strategies:
        endpoint = f"decision:{strategy}"
        try:
            decision_payloads[strategy] = _fetch_mapping(
                f"{base_url}/api/v2/decisions/{strategy}/current",
                timeout_seconds,
            )
        except FetchError as exc:
            issues.append(FetchIssue(endpoint, exc.error_code))
    return parse_web_sample(
        sample_number,
        datetime.now(_SHANGHAI).isoformat(),
        status_payload=status_payload,
        decision_payloads=decision_payloads,
        fetch_issues=tuple(issues),
    )


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
    )


def _parse_input_quality(payload: Mapping[str, object]) -> InputQualitySnapshot:
    return InputQualitySnapshot(
        status=_text(payload.get("status")),
        trade_date=_text(_mapping(payload.get("summary")).get("trade_date")),
        primary_blocker=_text(payload.get("primary_blocker")),
        funnel=_parse_funnel(_mapping(payload.get("supply_funnel"))),
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


class FetchError(RuntimeError):
    def __init__(self, error_code: str) -> None:
        super().__init__(error_code)
        self.error_code = error_code


def _fetch_mapping(url: str, timeout_seconds: float) -> Mapping[str, object]:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            payload = response.read(_MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise FetchError(f"http_{exc.code}") from exc
    except urllib.error.URLError as exc:
        raise FetchError("connection_failed") from exc
    except TimeoutError as exc:
        raise FetchError("request_timeout") from exc
    except OSError as exc:
        raise FetchError("request_os_error") from exc
    if len(payload) > _MAX_RESPONSE_BYTES:
        raise FetchError("response_too_large")
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FetchError("invalid_json") from exc
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise FetchError("invalid_json_root")
    return value


def build_report(
    base_url: str,
    samples: tuple[WebSample, ...],
    findings: tuple[Finding, ...],
    *,
    strategies: tuple[str, ...],
    consecutive_zero_threshold: int,
) -> dict[str, object]:
    error_count = sum(finding.severity == "error" for finding in findings)
    warning_count = sum(finding.severity == "warning" for finding in findings)
    return {
        "schema_version": "web_recommendation_health_v1",
        "status": "failed" if error_count else "degraded" if warning_count else "passed",
        "collected_at": datetime.now(_SHANGHAI).isoformat(),
        "target": base_url,
        "configuration": {
            "strategies": list(strategies),
            "sample_count": len(samples),
            "consecutive_zero_threshold": consecutive_zero_threshold,
        },
        "summary": {
            "successful_samples": sum(not sample.fetch_issues for sample in samples),
            "error_count": error_count,
            "warning_count": warning_count,
        },
        "findings": [_finding_payload(finding) for finding in findings],
        "samples": [_sample_payload(sample, strategies) for sample in samples],
    }


def _finding_payload(finding: Finding) -> dict[str, object]:
    return {
        "severity": finding.severity,
        "code": finding.code,
        "strategy": finding.strategy,
        "first_sample": finding.first_sample,
        "last_sample": finding.last_sample,
        "occurrences": finding.occurrences,
        "message": finding.message,
        "evidence": dict(finding.evidence),
    }


def _sample_payload(sample: WebSample, strategies: tuple[str, ...]) -> dict[str, object]:
    status = sample.status
    result: dict[str, object] = {}
    for strategy in strategies:
        current = sample.decisions.get(strategy)
        quality = _strategy_quality(sample, strategy)
        result[strategy] = {
            "status_projection": _projection_summary(status.strategies.get(strategy) if status is not None else None),
            "current_projection": _projection_summary(current),
            "current_item_count": current.item_count if current is not None else None,
            "empty_reason": current.empty_reason if current is not None else None,
            "input_quality_status": quality.status if quality is not None else None,
            "primary_blocker": quality.primary_blocker if quality is not None else None,
            "supply_funnel": _funnel_payload(quality.funnel if quality is not None else None),
        }
    return {
        "sample": sample.sample_number,
        "collected_at": sample.collected_at,
        "runtime_status": status.runtime_status if status is not None else None,
        "runtime_started": status.runtime_started if status is not None else False,
        "runtime_version": status.runtime_version if status is not None else None,
        "phase": status.phase if status is not None else None,
        "event_sequence": status.event_sequence if status is not None else None,
        "market": {
            "market_feature_rows": status.market_feature_rows if status is not None else None,
            "candidate_quote_cache_entries": status.candidate_quote_entries if status is not None else None,
            "candidate_quote_latest_source": status.candidate_quote_source if status is not None else None,
            "history_warmup": (
                {
                    "universe_rows": status.history_warmup.universe_rows,
                    "covered_rows": status.history_warmup.covered_rows,
                    "coverage_ratio": status.history_warmup.coverage_ratio,
                    "planned_count": status.history_warmup.planned_count,
                    "completed_count": status.history_warmup.completed_count,
                    "failure_count": status.history_warmup.failure_count,
                    "inflight_count": status.history_warmup.inflight_count,
                    "retry_deferred_count": status.history_warmup.retry_deferred_count,
                    "unique_failure_count": status.history_warmup.unique_failure_count,
                    "timeout_count": status.history_warmup.timeout_count,
                    "inflight_age_seconds": status.history_warmup.inflight_age_seconds,
                    "batch_timeout_seconds": status.history_warmup.batch_timeout_seconds,
                    "last_source": status.history_warmup.last_source,
                }
                if status is not None
                else None
            ),
        },
        "strategies": result,
        "fetch_issues": [{"endpoint": issue.endpoint, "error_code": issue.error_code} for issue in sample.fetch_issues],
    }


def _projection_summary(payload: ProjectionSnapshot | None) -> dict[str, object]:
    return {
        "status": payload.status if payload is not None else None,
        "trade_date": payload.trade_date if payload is not None else None,
        "projection_version": payload.projection_version if payload is not None else None,
        "candidate_count": payload.coverage.candidate_count if payload is not None else None,
        "evaluated_count": payload.coverage.evaluated_count if payload is not None else None,
        "selected_count": payload.coverage.selected_count if payload is not None else None,
    }


def _funnel_payload(payload: FunnelSnapshot | None) -> dict[str, int | None]:
    return {
        "requested_candidates": payload.requested_candidates if payload is not None else None,
        "candidate_features": payload.candidate_features if payload is not None else None,
        "security_master": payload.security_master if payload is not None else None,
        "history": payload.history if payload is not None else None,
        "full_scored": payload.full_scored if payload is not None else None,
        "selected_executable": payload.selected_executable if payload is not None else None,
        "selected_observe": payload.selected_observe if payload is not None else None,
    }


def _normalize_base_url(value: str) -> str:
    parsed = urllib.parse.urlsplit(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("--base-url must be an absolute HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
        raise ValueError("--base-url cannot contain credentials, query parameters, or fragments")
    path = parsed.path.rstrip("/")
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def _validate_args(args: argparse.Namespace) -> tuple[str, tuple[str, ...]]:
    if args.samples < 1:
        raise ValueError("--samples must be positive")
    if args.interval_seconds < 0.0:
        raise ValueError("--interval-seconds must not be negative")
    if args.timeout_seconds <= 0.0:
        raise ValueError("--timeout-seconds must be positive")
    if not 1 <= args.consecutive_zero_threshold <= args.samples:
        raise ValueError("--consecutive-zero-threshold must be between 1 and --samples")
    strategies = tuple(dict.fromkeys(args.strategies or _STRATEGIES))
    return _normalize_base_url(args.base_url), strategies


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): item for key, item in value.items() if isinstance(key, str)}


def _mapping_or_none(value: object) -> Mapping[str, object] | None:
    return _mapping(value) if isinstance(value, Mapping) else None


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _nonnegative_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _nonnegative_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value) or value < 0:
        return None
    return float(value)


def main() -> int:
    parser = _parser()
    args = parser.parse_args()
    try:
        base_url, strategies = _validate_args(args)
        samples = collect_samples(
            base_url,
            strategies=strategies,
            sample_count=args.samples,
            interval_seconds=args.interval_seconds,
            timeout_seconds=args.timeout_seconds,
        )
        findings = analyze_samples(
            samples,
            strategies=strategies,
            consecutive_zero_threshold=args.consecutive_zero_threshold,
        )
        report = build_report(
            base_url,
            samples,
            findings,
            strategies=strategies,
            consecutive_zero_threshold=args.consecutive_zero_threshold,
        )
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        report = {
            "schema_version": "web_recommendation_health_v1",
            "status": "failed",
            "error": type(exc).__name__,
        }
    emit_report(report)
    return 0 if report.get("status") in {"passed", "degraded"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
