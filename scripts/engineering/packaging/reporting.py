"""Installed-wheel public evidence projection."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from scripts.core.contracts import DiagnosticResult


def NormalizeResult(result: DiagnosticResult, *, local: bool = False) -> DiagnosticResult:
    return result


def CompleteReport(report: dict[str, object], results: Sequence[DiagnosticResult]) -> None:
    return None


def ProjectDetails(result: DiagnosticResult, source: Mapping[str, object], payload: dict[str, object]) -> None:
    fields = (
        "wheel", "resource_count", "installed_outside_repository", "research_evidence_commands_verified",
        "missing_history_read_only_smoke", "missing_selection_read_only_smoke",
    )
    payload["summary"] = {name: source[name] for name in fields if name in source}
