"""History diagnostic evidence projections."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence

from scripts.core.contracts import DiagnosticResult
from scripts.core.projection_values import (
    ProjectMapping,
    ProjectSummary,
    ProjectSummaryWithError,
    ProjectSummaryWithFindings,
    SafeText,
)


def NormalizeResult(result: DiagnosticResult, *, local: bool = False) -> DiagnosticResult:
    return result


def CompleteReport(report: dict[str, object], results: Sequence[DiagnosticResult]) -> None:
    return None


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
    payload["history_trading_sessions"] = source.get("history_trading_sessions")
    payload["timing_semantics"] = source.get("timing_semantics")
    payload["supplier_path"] = source.get("supplier_path")
    payload["security_metadata"] = source.get("security_metadata")
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
        "configured_request_timeout_seconds",
        "configured_price_budget_seconds",
        "configured_request_interval_seconds",
        "configured_retries",
        "requested_experiment_interval_seconds",
    )
    timing_fields = ("code", "phase", "elapsed_ms", "rows", "outcome", "error", "network_ms", "vendor_code", "raw_rows", "qfq_rows")
    payload["experiments"] = [
        {
            **{name: item.get(name) for name in fields},
            "request_timings": [
                {name: timing.get(name) for name in timing_fields}
                for timing in (item["request_timings"][:1000] if isinstance(item.get("request_timings"), list) else [])
                if isinstance(timing, dict)
            ],
        }
        for item in (values[:27] if isinstance(values, list) else [])
        if isinstance(item, dict)
    ]


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


_DETAILS: Mapping[str, Callable[[DiagnosticResult, Mapping[str, object], dict[str, object]], None]] = {
    "tencent_download": ProjectSummaryWithError,
    "tencent_history_window": ProjectSummaryWithError,
    "qfq_sqlite_audit": ProjectSummaryWithError,
    "download_failures": ProjectSummaryWithError,
    "history_daily_capability": _history_daily_capability_details,
    "baostock_concurrency": _baostock_concurrency_details,
    "history_publication_audit": ProjectSummaryWithFindings,
    "price_repair_audit": ProjectSummary,
    "baostock_qfq_shadow": _baostock_qfq_shadow_details,
}


def ProjectDetails(result: DiagnosticResult, source: Mapping[str, object], payload: dict[str, object]) -> None:
    _DETAILS.get(result.name, ProjectSummary)(result, source, payload)
