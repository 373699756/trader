"""Immutable execution and output values for the common tool router."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Literal

CheckStatus = Literal["passed", "degraded", "failed", "timed_out", "cancelled", "skipped", "empty"]


@dataclass(frozen=True)
class CommandPreparation:
    argv_suffix: tuple[str, ...] = ()
    environment: Mapping[str, str] | None = None
    report_metadata: Mapping[str, object] | None = None


class CommandPreparationError(ValueError):
    def __init__(self, error_code: str) -> None:
        super().__init__(error_code)
        self.error_code = error_code


@dataclass(frozen=True)
class DiagnosticCommand:
    name: str
    argv: tuple[str, ...]
    timeout_seconds: float
    output_kind: Literal["json", "text", "tool"] = "json"
    prepare: Callable[[], CommandPreparation] | None = None
    requires_success: tuple[str, ...] = ()


@dataclass(frozen=True)
class DiagnosticResult:
    name: str
    return_code: int
    duration_ms: float
    payload: Mapping[str, object] | None
    error_code: str | None


@dataclass(frozen=True)
class ToolOutcome:
    exit_code: int
    payload: Mapping[str, object]


Runner = Callable[[DiagnosticCommand], DiagnosticResult]
