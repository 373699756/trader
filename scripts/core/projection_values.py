from __future__ import annotations

from collections.abc import Mapping

from scripts.core.contracts import DiagnosticResult


def BoundedFindings(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)][:50]


def ProjectMapping(value: object) -> Mapping[str, object]:
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        return value
    return {}


def SafeStringList(value: object, *, limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item[:160] for item in value if isinstance(item, str)][:limit]


def SafeText(value: object) -> str | None:
    return value[:160] if isinstance(value, str) and value else None


def SafeNonnegativeInt(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def ProjectSummary(_result: DiagnosticResult, source: Mapping[str, object], payload: dict[str, object]) -> None:
    payload["summary"] = ProjectMapping(source.get("summary"))


def ProjectSummaryWithError(_result: DiagnosticResult, source: Mapping[str, object], payload: dict[str, object]) -> None:
    payload["summary"] = ProjectMapping(source.get("summary")) or {"error": source.get("error")}
    if source.get("benchmark_mode") is not None:
        payload["benchmark_mode"] = source["benchmark_mode"]


def ProjectSummaryWithFindings(result: DiagnosticResult, source: Mapping[str, object], payload: dict[str, object]) -> None:
    ProjectSummary(result, source, payload)
    payload["findings"] = BoundedFindings(source.get("findings"))
