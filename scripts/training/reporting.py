"""Training diagnostic evidence and benchmark completion projections."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from scripts.core.contracts import DiagnosticResult
from scripts.core.projection_values import ProjectMapping, ProjectSummary, ProjectSummaryWithError


def NormalizeResult(result: DiagnosticResult, *, local: bool = False) -> DiagnosticResult:
    return result


def ProjectDetails(result: DiagnosticResult, source: Mapping[str, object], payload: dict[str, object]) -> None:
    if result.name == "memory":
        fields = (
            "training_status", "repeat_training_status", "training_input_hash", "model_hash", "report_hash",
            "peak_rss_bytes", "starting_peak_rss_bytes", "max_rss_bytes", "sample_database_peak_bytes",
            "stage_durations_ms", "failure_reasons", "content_hash",
        )
        payload["summary"] = {name: source[name] for name in fields if name in source}
    elif result.name == "training_samples":
        ProjectSummary(result, source, payload)
    else:
        ProjectSummaryWithError(result, source, payload)


def CompleteReport(report: dict[str, object], results: Sequence[DiagnosticResult]) -> None:
    selection = ProjectMapping(report.get("selection"))
    if selection.get("module") != "resources" or selection.get("action") != "performance":
        return
    from scripts.training.jobs.training_performance import SummarizeTrainingBenchmark

    benchmark = SummarizeTrainingBenchmark(results)
    report["benchmark"] = benchmark
    if not benchmark["comparable"] and report["status"] == "passed":
        report["status"] = "degraded"
