"""Read-only Web and browser diagnostic projections."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence

from scripts.core.contracts import DiagnosticResult
from scripts.core.projection_values import (
    BoundedFindings,
    ProjectMapping,
    ProjectSummary,
    ProjectSummaryWithError,
    ProjectSummaryWithFindings,
    SafeNonnegativeInt,
    SafeStringList,
    SafeText,
)


def NormalizeResult(result: DiagnosticResult, *, local: bool = False) -> DiagnosticResult:
    return result


def CompleteReport(report: dict[str, object], results: Sequence[DiagnosticResult]) -> None:
    return None


def _web_health_details(_result: DiagnosticResult, source: Mapping[str, object], payload: dict[str, object]) -> None:
    if source.get("mode") == "complete_rounds":
        _complete_round_details(source, payload)
        return
    payload["summary"] = ProjectMapping(source.get("summary"))
    payload["findings"] = BoundedFindings(source.get("findings"))
    samples = source.get("samples")
    if isinstance(samples, list):
        payload["publication_samples"] = [
            {
                "sample": sample.get("sample"),
                "collected_at": sample.get("collected_at"),
                "strategies": {
                    strategy: {
                        key: ProjectMapping(ProjectMapping(ProjectMapping(sample.get("strategies")).get(strategy)).get("current_projection")).get(key)
                        for key in ("status", "trade_date", "projection_version", "evaluated_count", "selected_count")
                    }
                    for strategy in ("tomorrow", "d25")
                },
            }
            for sample in samples[-16:]
            if isinstance(sample, dict)
        ]
    if isinstance(samples, list) and samples and isinstance(samples[-1], dict):
        latest = samples[-1]
        payload["latest_runtime"] = {
            "runtime_status": latest.get("runtime_status"),
            "runtime_version": latest.get("runtime_version"),
            "run_mode": latest.get("run_mode"),
            "preview_state": latest.get("preview_state"),
            "preview_error": latest.get("preview_error"),
            "preview_started_at": latest.get("preview_started_at"),
            "recommendation_round": {
                key: value for key, value in latest["recommendation_round"].items()
                if key in {"sequence", "profiles", "outcome", "finished", "elapsed_ms", "cleanup_ms",
                           "pending_profiles", "running_profiles", "staged_profiles", "connection_attempts",
                           "statements", "worker_inflight", "worker_peak"}
            } if isinstance(latest.get("recommendation_round"), dict) else None,
            "stage_timings": [
                {key: timing.get(key) for key in ("stage", "maximum_ms", "p50_ms", "sample_count")}
                for timing in latest.get("stage_timings", [])[:64]
                if isinstance(timing, dict)
            ] if isinstance(latest.get("stage_timings"), list) else [],
            "phase": latest.get("phase"),
            "degraded_reasons": SafeStringList(latest.get("degraded_reasons"), limit=32),
            "recent_errors": [
                {
                    key: issue.get(key)
                    for key in (
                        "code",
                        "strategy",
                        "stage",
                        "severity",
                        "count",
                        "recovery_status",
                        "occurred_at",
                        "last_occurred_at",
                        "resolved_at",
                    )
                }
                for issue in latest.get("recent_errors", [])[:32]
                if isinstance(issue, dict)
            ]
            if isinstance(latest.get("recent_errors"), list)
            else [],
            "scoring_profile": _scoring_profile_summary(latest.get("scoring_profile")),
            "candidate_quote_age": ProjectMapping(ProjectMapping(latest.get("market")).get("candidate_quote_age")),
            "history_archive": ProjectMapping(ProjectMapping(latest.get("market")).get("history_archive")),
            "company_research": ProjectMapping(latest.get("company_research")),
            "strategies": ProjectMapping(latest.get("strategies")),
        }


def _complete_round_details(source: Mapping[str, object], payload: dict[str, object]) -> None:
    payload["mode"] = "complete_rounds"
    summary = ProjectMapping(source.get("summary"))
    payload["summary"] = {key: summary.get(key) for key in (
        "required_complete_rounds", "observed_batch_events", "complete_published_rounds", "identity_matched_rounds",
        "current_consecutive_qualified_rounds", "maximum_consecutive_qualified_rounds", "round_target_policy",
        "decision_source_age_qualified_rounds", "delivery_source_age_qualified_rounds", "source_age_qualified_rounds",
        "duplicate_rounds", "duplicate_events", "initial_replay_events", "http_poll_samples", "stop_reason", "scope",
        "performance_acceptance", "browser_acceptance", "freeze_recovery_acceptance",
    )}
    for section, keys in (
        ("configuration", ("observation_seconds", "minimum_poll_interval_seconds", "status_profile",
                           "quote_age_limit_seconds", "required_age_scope", "maximum_round_records", "maximum_stream_bytes")),
        ("runtime_identity", ("runtime_version", "session_generation", "initial_event_cursor", "last_event_cursor")),
        ("latest_runtime", ("phase", "round_sequence", "round_outcome")),
    ):
        values = ProjectMapping(source.get(section))
        payload[section] = {key: values.get(key) for key in keys}
    reasons = source.get("reasons")
    payload["reasons"] = [
        {key: item.get(key) for key in ("code", "count")}
        for raw in (reasons[:32] if isinstance(reasons, list) else ()) if (item := ProjectMapping(raw))
    ]
    rounds = source.get("rounds")
    projected_rounds: list[dict[str, object]] = []
    for raw in rounds[:64] if isinstance(rounds, list) else ():
        item = ProjectMapping(raw)
        projected = {key: item.get(key) for key in (
            "round_sequence", "event_id", "session_generation", "event_received_at", "outcome", "reason",
            "scheduler_elapsed_ms",
        )}
        heads = item.get("heads")
        for key in ("published_heads", "atomic_heads"):
            identities = item.get(key)
            projected[key] = [
                _complete_round_identity(ProjectMapping(value))
                for value in (identities[:8] if isinstance(identities, list) else ())
                if isinstance(value, dict)
            ]
        projected_heads: list[dict[str, object]] = []
        for raw_head in heads[:8] if isinstance(heads, list) else ():
            head = ProjectMapping(raw_head)
            projected_head = {key: head.get(key) for key in (
                "profile", "strategy", "stage", "publication_id", "projection_version", "observed_at", "trade_date",
                "input_identity_sha256", "selected_codes_sha256",
                "api_received_at", "selected_count", "evaluated_count", "quote_source_min", "quote_source_max",
                "decision_source_age_max_seconds", "event_delivery_source_age_max_seconds", "api_delivery_source_age_max_seconds",
            )}
            stages = head.get("stages")
            projected_head["stages"] = [
                _complete_round_stage(ProjectMapping(raw_stage))
                for raw_stage in (stages[:11] if isinstance(stages, list) else ())
                if isinstance(raw_stage, dict)
            ]
            projected_heads.append(projected_head)
        projected["heads"] = projected_heads
        projected_rounds.append(projected)
    payload["rounds"] = projected_rounds


def _complete_round_identity(value: Mapping[str, object]) -> dict[str, object]:
    return {key: value.get(key) for key in (
        "profile", "strategy", "stage", "publication_id", "projection_version", "observed_at", "trade_date",
        "input_identity_sha256", "selected_codes_sha256",
    )}


def _complete_round_stage(stage: Mapping[str, object]) -> dict[str, object]:
    result = {key: stage.get(key) for key in (
        "key", "input_count", "output_count", "rejected_count", "pending_count", "failed_count",
        "not_selected_count", "duration_ms",
    )}
    operations = stage.get("operation_durations")
    result["operation_durations"] = [
        {key: operation.get(key) for key in ("operation", "duration_ms")}
        for raw in (operations[:16] if isinstance(operations, list) else ()) if (operation := ProjectMapping(raw))
    ]
    return result


def _scoring_profile_summary(value: object) -> dict[str, object]:
    profile = ProjectMapping(value)
    heads = ProjectMapping(profile.get("heads"))
    projected_heads: dict[str, object] = {}
    for strategy in ("tomorrow", "d25"):
        head = ProjectMapping(heads.get(strategy))
        if not head:
            continue
        projected_heads[strategy] = {
            "profile_id": SafeText(head.get("profile_id")),
            "is_enable": head.get("is_enable") if isinstance(head.get("is_enable"), bool) else None,
            "model_id": SafeText(head.get("model_id")),
            "model_hash": SafeText(head.get("model_hash")),
            "score_scale": SafeText(head.get("score_scale")),
            "production_authority": head.get("production_authority")
            if isinstance(head.get("production_authority"), bool)
            else None,
            "training_label_cutoff": SafeText(head.get("training_label_cutoff")),
            "request_count": SafeNonnegativeInt(head.get("request_count")),
            "candidate_count": SafeNonnegativeInt(head.get("candidate_count")),
            "predictor_batch_count": SafeNonnegativeInt(head.get("predictor_batch_count")),
            "cache_hit_count": SafeNonnegativeInt(head.get("cache_hit_count")),
        }
    return {"profile_id": SafeText(profile.get("profile_id")), "heads": projected_heads}


def _browser_details(_result: DiagnosticResult, source: Mapping[str, object], payload: dict[str, object]) -> None:
    payload["summary"] = {
        "passed": source.get("passed"),
        "browser_dom": ProjectMapping(ProjectMapping(source.get("browser_dom")).get("summary")),
        "patch_to_paint": ProjectMapping(source.get("browser_patch_to_paint")),
        "freeze_patch": ProjectMapping(source.get("browser_freeze_patch")),
        "retention": ProjectMapping(source.get("web_snapshot_retention")),
    }


_DETAILS: Mapping[str, Callable[[DiagnosticResult, Mapping[str, object], dict[str, object]], None]] = {
    "web_health": _web_health_details,
    "browser_refresh": _browser_details,
    "browser_live": ProjectSummaryWithError,
    "company_ledger_browser": ProjectSummaryWithFindings,
}


def ProjectDetails(result: DiagnosticResult, source: Mapping[str, object], payload: dict[str, object]) -> None:
    _DETAILS.get(result.name, ProjectSummary)(result, source, payload)
