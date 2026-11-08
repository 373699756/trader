"""Startup, shutdown and read-only delivery for already composed profile systems."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Generic, Protocol, TypeVar

from trader.infra.settings import RuntimeSettings
from trader.infra.shutdown import (
    CollectShutdownFailures, ShutdownDeadline, ShutdownIncompleteError, ShutdownReport, ShutdownStep,
)
from trader.recommendation.domain.scoring.profile_identity import ScoringProfileId


class ProfileRuntime(Protocol):
    @property
    def profile_id(self) -> ScoringProfileId: ...

    def initialize(self) -> None: ...

    def start(self) -> bool: ...

    def stop(self, *, deadline: ShutdownDeadline | None = None) -> ShutdownReport: ...


ApplicationT = TypeVar("ApplicationT")


@dataclass(frozen=True)
class ProfiledApplicationSystem(Generic[ApplicationT]):
    """One process hosting the selected, isolated recommendation profiles."""

    settings: RuntimeSettings
    app: ApplicationT
    systems: tuple[ProfileRuntime, ...]

    def start(self) -> bool:
        prepared: list[ProfileRuntime] = []
        try:
            for system in self.systems:
                prepared.append(system)
                system.initialize()
            for system in self.systems:
                if not system.start():
                    raise RuntimeError(f"{system.profile_id} runtime did not start")
        except BaseException as exc:
            deadline = exc.deadline if isinstance(exc, ShutdownIncompleteError) else (
                ShutdownDeadline.start(self.settings.pipeline.shutdown_timeout_seconds)
            )
            report = self._stop_profiles(tuple(prepared), deadline)
            if not report.completed and isinstance(exc, Exception):
                raise ShutdownIncompleteError(report, deadline) from exc
            raise
        return True

    def stop(self, *, deadline: ShutdownDeadline | None = None) -> ShutdownReport:
        shared = deadline or ShutdownDeadline.start(self.settings.pipeline.shutdown_timeout_seconds)
        return self._stop_profiles(self.systems, shared)

    def _stop_profiles(self, systems: tuple[ProfileRuntime, ...], deadline: ShutdownDeadline) -> ShutdownReport:
        steps: list[ShutdownStep] = []
        forced = False
        for system in reversed(systems):
            with CollectShutdownFailures(deadline, steps, f"profile:{system.profile_id}"):
                report = system.stop(deadline=deadline)
                forced = forced or report.forced
                steps.extend(replace(step, name=f"{system.profile_id}:{step.name}") for step in report.steps)
                if not report.completed and all(step.completed for step in report.steps):
                    steps.append(ShutdownStep(f"profile:{system.profile_id}", False, report.forced,
                                              detail="profile_shutdown_incomplete"))
        return ShutdownReport.from_steps(deadline, steps, forced=forced or deadline.expired)


__all__ = ["ProfileRuntime", "ProfiledApplicationSystem"]
