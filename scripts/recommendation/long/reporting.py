"""Long watchlist admission result projection."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence

from scripts.core.contracts import DiagnosticResult
from scripts.core.projection_values import ProjectMapping, ProjectSummary


def NormalizeResult(result: DiagnosticResult, *, local: bool = False) -> DiagnosticResult:
    return result


def CompleteReport(report: dict[str, object], results: Sequence[DiagnosticResult]) -> None:
    return None


def _long_admission_details(
    _result: DiagnosticResult, source: Mapping[str, object], payload: dict[str, object]
) -> None:
    summary = ProjectMapping(source.get("summary"))
    payload["summary"] = {
        key: summary.get(key) for key in ("requested", "retained", "excluded", "pending", "collected_at", "coverage")
    }


_DETAILS: Mapping[str, Callable[[DiagnosticResult, Mapping[str, object], dict[str, object]], None]] = {
    "long_watchlist_admission": _long_admission_details,
}


def ProjectDetails(result: DiagnosticResult, source: Mapping[str, object], payload: dict[str, object]) -> None:
    _DETAILS.get(result.name, ProjectSummary)(result, source, payload)
