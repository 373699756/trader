"""Technical diagnostic result projection for this owner."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from scripts.core.contracts import DiagnosticResult
from scripts.core.projection_values import ProjectSummaryWithError


def NormalizeResult(result: DiagnosticResult, *, local: bool = False) -> DiagnosticResult:
    return result


def CompleteReport(report: dict[str, object], results: Sequence[DiagnosticResult]) -> None:
    return None


def ProjectDetails(result: DiagnosticResult, source: Mapping[str, object], payload: dict[str, object]) -> None:
    ProjectSummaryWithError(result, source, payload)
