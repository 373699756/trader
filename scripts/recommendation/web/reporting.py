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
