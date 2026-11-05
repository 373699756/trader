"""Market diagnostics, source evidence and the explicitly selected text view."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace

from scripts.core.contracts import DiagnosticResult
from scripts.core.projection_values import (
    ProjectMapping,
    ProjectSummary,
    ProjectSummaryWithError,
    ProjectSummaryWithFindings,
)


def NormalizeResult(result: DiagnosticResult, *, local: bool = False) -> DiagnosticResult:
    source = result.payload
    if (
        result.name == "tencent_quotes" and result.error_code is None and source is not None
        and source.get("status") is None and source.get("schema_version") == "tencent-quote-sampling"
        and not isinstance(source.get("passed"), bool)
    ):
        return replace(result, payload=dict(source, passed=True))
    return result


def CompleteReport(report: dict[str, object], results: Sequence[DiagnosticResult]) -> None:
    return None


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


_DETAILS: Mapping[str, Callable[[DiagnosticResult, Mapping[str, object], dict[str, object]], None]] = {
    "exchange_security_master": ProjectSummaryWithError,
    "tencent_transport": ProjectSummaryWithError,
    "tencent_quotes": _tencent_quote_details,
    "tushare_daily": _tushare_details,
    "market_factor_compare": ProjectSummaryWithFindings,
}


def ProjectDetails(result: DiagnosticResult, source: Mapping[str, object], payload: dict[str, object]) -> None:
    _DETAILS.get(result.name, ProjectSummary)(result, source, payload)


def RenderText(report: Mapping[str, object]) -> str:
    from scripts.download.market.market_factor_text import RenderMarketFactorText

    return RenderMarketFactorText(report)
