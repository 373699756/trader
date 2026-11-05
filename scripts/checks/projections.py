from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import datetime
from typing import cast
from zoneinfo import ZoneInfo

from scripts.checks.contracts import CheckStatus, DiagnosticResult
from scripts.checks.catalog import CheckByIdentity
from scripts.checks.download.company.projections import ProjectCompanyResume
from scripts.checks.projection_values import BoundedFindings, ProjectMapping, SafeText, SafeNonnegativeInt, SafeStringList

_SHANGHAI = ZoneInfo("Asia/Shanghai")

def BuildReport(check_id: str, results: Sequence[DiagnosticResult]) -> dict[str, object]:
    checks = [_check_payload(result, local=check_id == "snapshot") for result in results]
    statuses = [check["status"] for check in checks]
    summary = {
        "passed": statuses.count("passed"),
        "degraded": statuses.count("degraded"),
        **{status: statuses.count(status) for status in ("timed_out", "cancelled", "skipped", "empty")},
        "failed": statuses.count("failed"),
        "total": len(statuses),
    }
    report: dict[str, object] = {
        "schema_version": "trader-runtime-diagnostics",
        "status": "failed" if summary["failed"] or summary["timed_out"] or summary["cancelled"] else "degraded" if summary["degraded"] or summary["skipped"] else "empty" if all(status == "empty" for status in statuses) else "passed",
        "collected_at": datetime.now(_SHANGHAI).isoformat(),
        "selection": CheckByIdentity(check_id).projection(),
        "summary": summary,
        "findings": [finding for check in checks for finding in _findings(check)],
        "checks": checks,
    }
    if check_id == "training-performance":
        from scripts.checks.training.training_performance import SummarizeTrainingBenchmark

        benchmark = SummarizeTrainingBenchmark(results)
        report["benchmark"] = benchmark
        if not benchmark["comparable"] and report["status"] == "passed":
            report["status"] = "degraded"
    return report


def _check_payload(result: DiagnosticResult, *, local: bool = False) -> dict[str, object]:
    original_name = result.name
    result = replace(result, name=result.name.split(":")[0])
    if (
        result.error_code is None and result.return_code == 0 and result.payload is not None
        and result.payload.get("status") == "skipped" and result.payload.get("executed") is False
    ):
        return {
            "name": original_name,
            "status": "skipped",
            "executed": False,
            "reason": result.payload.get("reason"),
            "duration_ms": 0.0,
        }
    status = ResultStatus(result)
    if (
        local
        and result.name == "research_readiness"
        and result.error_code is None
        and result.payload is not None
        and _valid_research_status(result.payload)
        and result.return_code in (0, 1)
        and status == "failed"
    ):
        status = "degraded"
    payload: dict[str, object] = {
        "name": original_name,
        "status": status,
        "executed": True,
        "duration_ms": result.duration_ms,
        "schema_version": result.payload.get("schema_version") if result.payload is not None else None,
    }
    if result.error_code is not None:
        payload["error_code"] = result.error_code
    source = result.payload or {}
    handler = _CHECK_DETAILS.get(result.name)
    if handler is not None:
        handler(result, source, payload)
    else:
        payload["summary"] = ProjectMapping(source.get("summary"))
    findings = payload.get("findings")
    if local and status == "degraded" and isinstance(findings, list):
        payload["findings"] = [dict(item, severity="warning") for item in findings if isinstance(item, dict)]
    return payload


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


def _history_details(_result: DiagnosticResult, source: Mapping[str, object], payload: dict[str, object]) -> None:
    payload["summary"] = ProjectMapping(source.get("summary"))


def _security_master_details(
    _result: DiagnosticResult, source: Mapping[str, object], payload: dict[str, object]
) -> None:
    payload["summary"] = ProjectMapping(source.get("summary")) or {"error": source.get("error")}
    if source.get("benchmark_mode") is not None:
        payload["benchmark_mode"] = source["benchmark_mode"]


def _tencent_quote_details(_result: DiagnosticResult, source: Mapping[str, object], payload: dict[str, object]) -> None:
    versions = ProjectMapping(source.get("distinct_source_versions"))
    payload["summary"] = {
        "latency": ProjectMapping(source.get("latency")),
        "tracked_codes": len(versions),
        "changing_codes": sum(isinstance(value, int) and value > 1 for value in versions.values()),
        "source_changed": source.get("source_changed"),
    }


def _tushare_details(_result: DiagnosticResult, source: Mapping[str, object], payload: dict[str, object]) -> None:
    summary = ProjectMapping(source.get("summary"))
    payload["summary"] = {
        "requested_codes": summary.get("requested_codes"),
        "successful_codes": summary.get("successful_codes"),
        "latency_ms": summary.get("latency_ms"),
        "degraded_reason": summary.get("degraded_reason"),
        "capability": ProjectMapping(source.get("capability")),
        "usage": ProjectMapping(source.get("usage")),
    }


def _history_daily_capability_details(
    _result: DiagnosticResult,
    source: Mapping[str, object],
    payload: dict[str, object],
) -> None:
    candidates = source.get("candidates")
    candidate_values = candidates if isinstance(candidates, list) else []
    payload["summary"] = {
        "freeze": ProjectMapping(source.get("freeze")),
        "baostock_lower_bound": ProjectMapping(source.get("baostock_lower_bound")),
        "candidates": [
            {
                "source": item.get("source"),
                "request_scope": item.get("request_scope"),
                "market_day_batch": item.get("market_day_batch"),
                "market_day_raw": item.get("market_day_raw"),
                "adjustment_factor": item.get("adjustment_factor"),
                "raw_qfq_semantics_observed": item.get("raw_qfq_semantics_observed"),
                "probe_status": item.get("probe_status"),
                "probe_error": item.get("probe_error"),
            }
            for item in candidate_values[:10]
            if isinstance(item, dict)
        ],
    }


def _baostock_concurrency_details(
    _result: DiagnosticResult, source: Mapping[str, object], payload: dict[str, object]
) -> None:
    payload["production_eligible"] = False
    payload["parallel_sessions"] = source.get("parallel_sessions")
    payload["history_calendar_days"] = source.get("history_calendar_days")
    payload["rate_semantics"] = source.get("rate_semantics")
    payload["retry_policy"] = source.get("retry_policy")
    payload["error"] = SafeText(source.get("error"))
    values = source.get("experiments")
    fields = (
        "mode",
        "sample_size",
        "success_count",
        "failure_count",
        "success_rate",
        "network_receive_errors",
        "timeouts",
        "retries",
        "raw_rows",
        "qfq_rows",
        "raw_qfq_inconsistencies",
        "error_categories",
        "elapsed_ms",
        "average_code_ms",
        "successful_codes_per_minute",
        "interval_seconds",
        "round",
    )
    payload["experiments"] = [
        {name: item.get(name) for name in fields}
        for item in (values[:27] if isinstance(values, list) else [])
        if isinstance(item, dict)
    ]


def _history_sqlite_performance_details(
    _result: DiagnosticResult,
    source: Mapping[str, object],
    payload: dict[str, object],
) -> None:
    payload["summary"] = ProjectMapping(source.get("summary"))


def _summary_with_findings(
    _result: DiagnosticResult,
    source: Mapping[str, object],
    payload: dict[str, object],
) -> None:
    payload["summary"] = ProjectMapping(source.get("summary"))
    payload["findings"] = BoundedFindings(source.get("findings"))


def _research_source_compare_details(
    _result: DiagnosticResult,
    source: Mapping[str, object],
    payload: dict[str, object],
) -> None:
    payload["market_pagination"] = ProjectMapping(source.get("market_pagination"))
    payload["structured_samples"] = source.get("structured_samples", [])
    sources = source.get("sources")
    source_values = sources if isinstance(sources, list) else []
    projected_sources: list[dict[str, object]] = []
    for item in source_values[:4]:
        if not isinstance(item, dict):
            continue
        samples = item.get("samples")
        sample_values = samples if isinstance(samples, list) else []
        missing_field_counts: dict[str, int] = {}
        component_error_counts: dict[str, int] = {}
        for sample in sample_values[:50]:
            if not isinstance(sample, dict):
                continue
            for field in SafeStringList(sample.get("missing_fields"), limit=12):
                missing_field_counts[field] = missing_field_counts.get(field, 0) + 1
            for error in SafeStringList(sample.get("component_errors"), limit=12):
                component_error_counts[error] = component_error_counts.get(error, 0) + 1
        projected_sources.append(
            {
                "source": SafeText(item.get("source")),
                "tested": item.get("tested") is True,
                "excluded_by_budget": item.get("excluded_by_budget") is True,
                "physical_http_requests": SafeNonnegativeInt(item.get("physical_http_requests")),
                "sample_count": SafeNonnegativeInt(item.get("sample_count")),
                "field_coverage": ProjectMapping(item.get("field_coverage")),
                "interface_status": SafeText(item.get("interface_status")),
                "production_role": SafeText(item.get("production_role")),
                "regulatory_history_complete": item.get("regulatory_history_complete") is True,
                "exchange_cross_check_status": SafeText(item.get("exchange_cross_check_status")),
                "missing_field_counts": missing_field_counts,
                "component_error_counts": component_error_counts,
            }
        )
    payload["summary"] = {
        "required_fields": SafeStringList(source.get("required_fields"), limit=12),
        "sources": projected_sources,
        "selected_route": ProjectMapping(source.get("selected_route")),
        "budget": ProjectMapping(source.get("budget")),
    }


def _baostock_qfq_shadow_details(
    _result: DiagnosticResult,
    source: Mapping[str, object],
    payload: dict[str, object],
) -> None:
    payload["production_eligible"] = source.get("production_eligible") is True
    payload["summary"] = {
        "published_snapshot": ProjectMapping(source.get("published_snapshot")),
        "target_window": ProjectMapping(source.get("target_window")),
        "raw_qfq_integrity": ProjectMapping(source.get("raw_qfq_integrity")),
        "request_baseline": ProjectMapping(source.get("request_baseline")),
        "production_eligibility_reason": source.get("production_eligibility_reason"),
    }


def _research_details(result: DiagnosticResult, source: Mapping[str, object], payload: dict[str, object]) -> None:
    if result.payload is not None and not _valid_research_status(source):
        payload["findings"] = [
            {
                "severity": "error",
                "code": "research_status_shape_invalid",
                "message": "The research status schema or active window projection is invalid.",
            }
        ]
        return
    history = ProjectMapping(source.get("history_archive"))
    tomorrow = ProjectMapping(source.get("tomorrow_research"))
    input_blockers = SafeStringList(tomorrow.get("input_blockers"), limit=20)
    production_blockers = SafeStringList(tomorrow.get("production_blockers"), limit=20)
    target = ProjectMapping(source.get("target_selection"))
    target_blockers = SafeStringList(target.get("blockers"), limit=20)
    blockers = target_blockers + input_blockers + production_blockers
    if blockers:
        payload["findings"] = [
            {
                "severity": "error",
                "code": blockers[0],
                "message": "Research requires qualified evidence for its declared selection identity.",
                "evidence": {
                    "target_blockers": target_blockers,
                    "input_blockers": input_blockers,
                    "production_blockers": production_blockers,
                    "production_authority": tomorrow.get("production_authority"),
                },
            }
        ]
    payload["summary"] = {
        "target_selection": {
            "research_identity": target.get("research_identity"),
            "status": target.get("status"),
            "strategies": SafeStringList(target.get("strategies"), limit=2),
            "population": target.get("population"),
            "validation_command": target.get("validation_command"),
            "blockers": target_blockers,
            "production_authority": target.get("production_authority"),
        },
        "history_archive": {
            "state": history.get("state"),
            "snapshot_hash": history.get("snapshot_hash"),
            "data_cutoff": history.get("data_cutoff"),
            "label_cutoff": history.get("label_cutoff"),
            "calendar_sessions": history.get("calendar_sessions"),
            "universe_count": history.get("universe_count"),
            "partition_count": history.get("partition_count"),
            "reason": history.get("reason"),
            "production_authority": history.get("production_authority"),
            "point_in_time_parity": history.get("point_in_time_parity"),
        },
        "v3": {
            "status": tomorrow.get("status"),
            "next_stage": tomorrow.get("next_stage"),
            "input_prerequisite_status": tomorrow.get("input_prerequisite_status"),
            "input_blockers": input_blockers,
            "production_blockers": production_blockers,
            "production_readiness": tomorrow.get("production_readiness"),
            "production_authority": tomorrow.get("production_authority"),
        },
        "production_authority": tomorrow.get("production_authority"),
    }


def _browser_details(_result: DiagnosticResult, source: Mapping[str, object], payload: dict[str, object]) -> None:
    payload["summary"] = {
        "passed": source.get("passed"),
        "browser_dom": ProjectMapping(ProjectMapping(source.get("browser_dom")).get("summary")),
        "patch_to_paint": ProjectMapping(source.get("browser_patch_to_paint")),
        "freeze_patch": ProjectMapping(source.get("browser_freeze_patch")),
        "retention": ProjectMapping(source.get("web_snapshot_retention")),
    }


def _performance_details(_result: DiagnosticResult, source: Mapping[str, object], payload: dict[str, object]) -> None:
    payload["summary"] = {
        "workload": ProjectMapping(source.get("workload")),
        "measurements": ProjectMapping(source.get("measurements")),
        "memory": ProjectMapping(source.get("memory")),
        "identity": ProjectMapping(source.get("identity")),
        "network_calls": source.get("network_calls"),
        "model_workload": ProjectMapping(source.get("model_workload")),
        "profile_results": ProjectMapping(source.get("profile_results")),
        "failures": SafeStringList(source.get("failures"), limit=30),
    }
    if isinstance(source.get("continuous_pipeline"), Mapping):
        summary = cast(dict[str, object], payload["summary"])
        summary["continuous_pipeline"] = ProjectMapping(source["continuous_pipeline"])


def _long_admission_details(
    _result: DiagnosticResult, source: Mapping[str, object], payload: dict[str, object]
) -> None:
    summary = ProjectMapping(source.get("summary"))
    payload["summary"] = {
        key: summary.get(key) for key in ("requested", "retained", "excluded", "pending", "collected_at", "coverage")
    }


def _configuration_details(_result: DiagnosticResult, source: Mapping[str, object], payload: dict[str, object]) -> None:
    payload["summary"] = {
        key: source.get(key)
        for key in (
            "status",
            "scoring_profile",
            "runtime_version",
            "strategy_version",
            "history_sessions",
            "training_fit_dates",
            "research_arms",
            "failure",
            "category",
        )
    }


def _maintenance_details(_result: DiagnosticResult, source: Mapping[str, object], payload: dict[str, object]) -> None:
    payload["summary"] = {
        key: source.get(key)
        for key in (
            "state",
            "reason",
            "data_cutoff",
            "label_cutoff",
            "training_due",
            "training_due_reason",
            "reminder_state",
        )
    }


def _native_details(result: DiagnosticResult, source: Mapping[str, object], payload: dict[str, object]) -> None:
    fields = (
        ("wheel", "resource_count", "installed_outside_repository", "research_evidence_commands_verified",
         "missing_history_read_only_smoke", "missing_selection_read_only_smoke")
        if result.name == "wheel-install"
        else ("training_status", "repeat_training_status", "training_input_hash", "model_hash", "report_hash",
              "peak_rss_bytes", "starting_peak_rss_bytes", "max_rss_bytes", "sample_database_peak_bytes",
              "stage_durations_ms", "failure_reasons", "content_hash")
    )
    payload["summary"] = {name: source[name] for name in fields if name in source}


_CHECK_DETAILS: Mapping[str, Callable[[DiagnosticResult, Mapping[str, object], dict[str, object]], None]] = {
    "wheel-install": _native_details,
    "memory": _native_details,
    "disk_profile": _security_master_details,
    "v5_samples": _security_master_details,
    "v5_lite": _security_master_details,
    "configuration": _configuration_details,
    "history_maintenance": _maintenance_details,
    "long_watchlist_admission": _long_admission_details,
    "web_health": _web_health_details,
    "history_sources": _history_details,
    "tencent_download": _security_master_details,
    "tencent_history_window": _security_master_details,
    "v5_training": _security_master_details,
    "training_alignment": _security_master_details,
    "training_resources": _security_master_details,
    "training_samples": _history_sqlite_performance_details,
    "training_coverage": _security_master_details,
    "model_inputs": _security_master_details,
    "qfq_sqlite_audit": _security_master_details,
    "download_failures": _security_master_details,
    "exchange_security_master": _security_master_details,
    "tencent_quotes": _tencent_quote_details,
    "tencent_transport": _security_master_details,
    "tushare_daily": _tushare_details,
    "history_daily_capability": _history_daily_capability_details,
    "baostock_concurrency": _baostock_concurrency_details,
    "history_sqlite_performance": _history_sqlite_performance_details,
    "history_publication_audit": _summary_with_findings,
    "history_source_compare": _history_sqlite_performance_details,
    "price_repair_audit": _history_sqlite_performance_details,
    "market_factor_compare": _summary_with_findings,
    "company_ledger_browser": _summary_with_findings,
    "research_source_compare": _research_source_compare_details,
    "history_write_benchmark": _history_sqlite_performance_details,
    "download_performance": _history_sqlite_performance_details,
    "database_baseline": _history_sqlite_performance_details,
    "company_resume": ProjectCompanyResume,
    "history_space": _history_sqlite_performance_details,
    "history_industry": _history_sqlite_performance_details,
    "baostock_qfq_shadow": _baostock_qfq_shadow_details,
    "research_readiness": _research_details,
    "browser_refresh": _browser_details,
    "browser_live": _security_master_details,
    "production_performance": _performance_details,
}


def ResultStatus(result: DiagnosticResult) -> CheckStatus:
    if result.error_code == "command_timeout":
        return "timed_out"
    if result.error_code == "command_cancelled":
        return "cancelled"
    if result.error_code is None and result.payload is not None:
        # Workers can handle their own signal/deadline and still emit a report.
        if result.payload.get("status") in ("timed_out", "cancelled"):
            return cast(CheckStatus, result.payload["status"])
    if result.error_code is not None or not result.payload or result.return_code != 0:
        return "failed"
    if result.name == "research_readiness" and not _valid_research_status(result.payload):
        return "failed"
    if result.name == "research_readiness":
        if SafeStringList(result.payload.get("blockers"), limit=1):
            return "failed"
        tomorrow = ProjectMapping(result.payload.get("tomorrow_research"))
        if tomorrow.get("status") == "blocked":
            return "failed"
        if SafeStringList(tomorrow.get("input_blockers"), limit=1) or SafeStringList(
            tomorrow.get("production_blockers"), limit=1
        ):
            return "failed"
    status = result.payload.get("status")
    if status in ("skipped", "empty", "timed_out", "cancelled"):
        return status
    if status == "passed":
        return "passed"
    if status == "degraded":
        return "degraded"
    if status == "failed":
        return "failed"
    passed = result.payload.get("passed")
    if isinstance(passed, bool):
        return "passed" if passed else "failed"
    if result.name == "configuration" and status == "ok":
        return "passed"
    if result.name in ("research_readiness", "history_maintenance") and status is None:
        return "passed"
    if result.name == "tencent_quotes" and status is None and result.payload.get("schema_version") == "tencent-quote-sampling":
        return "passed"
    return "failed"


def _valid_research_status(payload: Mapping[str, object]) -> bool:
    history = payload.get("history_archive")
    tomorrow = payload.get("tomorrow_research")
    if not isinstance(history, dict) or not isinstance(tomorrow, dict):
        return False
    if payload.get("production_authority") is not False:
        return False
    if history.get("production_authority") is not False or history.get("point_in_time_parity") is not False:
        return False
    if tomorrow.get("production_authority") is not False:
        return False
    if not all(
        isinstance(history.get(name), (str, int))
        for name in (
            "state",
            "calendar_sessions",
            "universe_count",
            "partition_count",
        )
    ):
        return False
    if history.get("reason") is not None and not isinstance(history.get("reason"), str):
        return False
    if not isinstance(tomorrow.get("status"), str) or not isinstance(tomorrow.get("production_readiness"), str):
        return False
    if not all(
        isinstance(tomorrow.get(name), list) and all(isinstance(item, str) for item in tomorrow[name])
        for name in ("input_blockers", "production_blockers")
    ):
        return False
    return payload.get("schema_version") == "research_readiness"


def _findings(check: Mapping[str, object]) -> list[dict[str, object]]:
    existing = check.get("findings")
    if isinstance(existing, list):
        return [dict(item, check=check["name"]) for item in existing if isinstance(item, dict)][:50]
    status = check.get("status")
    if status == "passed":
        return []
    return [
        {
            "severity": "error" if status in {"failed", "timed_out", "cancelled"} else "warning",
            "code": f"{check['name']}_{status}",
            "check": check["name"],
            "evidence": check.get("summary", {"error_code": check.get("error_code")}),
        }
    ]
