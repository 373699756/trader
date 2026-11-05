"""Hard wall-clock budgets for live market-factor diagnostics."""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

MARKET_SOURCE_LOOP_LIMIT_SECONDS = 120.0
MARKET_TASK_LIMIT_SECONDS = 600.0


@dataclass(frozen=True)
class SourceExclusion:
    source: str
    reason: str
    elapsed_ms: float
    limit_ms: float
    route: str


@dataclass
class DiagnosticBudget:
    source_loop_seconds: float
    task_seconds: float
    monotonic: Callable[[], float] = time.monotonic
    started_at: float = field(init=False)
    source_elapsed_ms: dict[str, float] = field(default_factory=dict, init=False)
    exclusions: dict[str, SourceExclusion] = field(default_factory=dict, init=False)
    task_timed_out: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        self.started_at = self.monotonic()

    def route_allowed(self, sources: Sequence[str]) -> bool:
        return not any(source in self.exclusions for source in sources)

    def source_remaining_seconds(self, source: str) -> float:
        return max(0.0, self.source_loop_seconds - self.source_elapsed_ms.get(source, 0.0) / 1000.0)

    def task_deadline(self) -> float:
        return self.started_at + self.task_seconds

    def record_source(self, source: str, elapsed_ms: float, route: str) -> None:
        total_ms = self.source_elapsed_ms.get(source, 0.0) + max(0.0, elapsed_ms)
        self.source_elapsed_ms[source] = total_ms
        limit_ms = self.source_loop_seconds * 1000.0
        if total_ms >= limit_ms and source not in self.exclusions:
            self.exclusions[source] = SourceExclusion(
                source=source,
                reason="source_loop_timeout",
                elapsed_ms=round(total_ms, 3),
                limit_ms=round(limit_ms, 3),
                route=route,
            )

    def task_expired(self, sources: Sequence[str] = (), route: str = "") -> bool:
        elapsed_ms = (self.monotonic() - self.started_at) * 1000.0
        limit_ms = self.task_seconds * 1000.0
        if elapsed_ms < limit_ms:
            return False
        self.task_timed_out = True
        for source in sources:
            if source not in self.exclusions:
                self.exclusions[source] = SourceExclusion(
                    source=source,
                    reason="diagnostic_task_timeout",
                    elapsed_ms=round(elapsed_ms, 3),
                    limit_ms=round(limit_ms, 3),
                    route=route,
                )
        return True

    def report(self) -> dict[str, object]:
        elapsed_ms = round((self.monotonic() - self.started_at) * 1000.0, 3)
        return {
            "source_loop_limit_seconds": self.source_loop_seconds,
            "task_limit_seconds": self.task_seconds,
            "task_elapsed_ms": elapsed_ms,
            "task_timed_out": self.task_timed_out,
            "source_elapsed_ms": {source: round(value, 3) for source, value in sorted(self.source_elapsed_ms.items())},
            "excluded_sources": [
                {
                    "source": item.source,
                    "reason": item.reason,
                    "elapsed_ms": item.elapsed_ms,
                    "limit_ms": item.limit_ms,
                    "route": item.route,
                }
                for item in sorted(self.exclusions.values(), key=lambda value: value.source)
            ],
        }


__all__ = [
    "DiagnosticBudget",
    "MARKET_SOURCE_LOOP_LIMIT_SECONDS",
    "MARKET_TASK_LIMIT_SECONDS",
    "SourceExclusion",
]
