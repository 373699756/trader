"""Lifecycle and read-only delivery for already composed profile systems."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Generic, Protocol, TypeVar

from trader.infra.settings import RuntimeSettings
from trader.infra.shutdown import ShutdownDeadline, ShutdownReport, ShutdownStep
from trader.recommendation.domain.scoring.profile_identity import ScoringProfileId


class ProfileRuntime(Protocol):
    @property
    def profile_id(self) -> ScoringProfileId: ...

    def start(self) -> bool: ...

    def stop(self, *, deadline: ShutdownDeadline | None = None) -> ShutdownReport: ...


ApplicationT = TypeVar("ApplicationT")


@dataclass(frozen=True)
class ProfiledApplicationSystem(Generic[ApplicationT]):
    """One process hosting isolated V2 and V3 recommendation lanes."""

    settings: RuntimeSettings
    app: ApplicationT
    systems: tuple[ProfileRuntime, ...]

    def start(self) -> bool:
        started: list[ProfileRuntime] = []
        try:
            for system in self.systems:
                if not system.start():
                    raise RuntimeError(f"{system.profile_id} runtime did not start")
                started.append(system)
        except BaseException:
            deadline = ShutdownDeadline.start(self.settings.pipeline.shutdown_timeout_seconds)
            for system in reversed(started):
                system.stop(deadline=deadline)
            raise
        return True

    def stop(self, *, deadline: ShutdownDeadline | None = None) -> ShutdownReport:
        shared = deadline or ShutdownDeadline.start(self.settings.pipeline.shutdown_timeout_seconds)
        steps: list[ShutdownStep] = []
        forced = False
        for system in reversed(self.systems):
            report = system.stop(deadline=shared)
            forced = forced or report.forced
            steps.extend(replace(step, name=f"{system.profile_id}:{step.name}") for step in report.steps)
        return ShutdownReport.from_steps(shared, steps, forced=forced or shared.expired)


__all__ = ["ProfileRuntime", "ProfiledApplicationSystem"]
