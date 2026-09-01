"""Advance the immutable Tomorrow research graph by at most one stage."""

from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass
from typing import Literal, Protocol

from trader.application.research.replay_models import canonical_hash
from trader.application.research.tomorrow_research_artifacts import (
    TomorrowProductionReadinessAudit,
    TomorrowResearchArtifactGraph,
    TomorrowResearchStage,
    TomorrowResearchStageHandoff,
    derive_tomorrow_research_run_id,
    next_research_stage,
    production_readiness_audit,
)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class TomorrowResearchOrchestrationStore(Protocol):
    def load_graph(self) -> TomorrowResearchArtifactGraph: ...

    def load_handoff(self, stage: TomorrowResearchStage) -> TomorrowResearchStageHandoff | None: ...

    def commit(
        self,
        expected_graph_hash: str,
        handoff: TomorrowResearchStageHandoff,
    ) -> TomorrowResearchArtifactGraph: ...


class TomorrowResearchProgressPort(Protocol):
    def update(self, stage: TomorrowResearchStage, status: Literal["blocked", "sealing", "completed"]) -> None: ...


@dataclass(frozen=True)
class TomorrowResearchPrerequisite:
    status: Literal["ready", "blocked"]
    prerequisite_hash: str
    blockers: tuple[str, ...] = ()
    schema_version: str = "tomorrow_research_prerequisite_v1"
    production_authority: bool = False
    content_hash: str = dataclasses.field(init=False)

    def __post_init__(self) -> None:
        blockers = tuple(sorted(set(self.blockers)))
        if _SHA256.fullmatch(self.prerequisite_hash) is None:
            raise ValueError("Tomorrow research prerequisite hash is invalid")
        if any(re.fullmatch(r"[a-z0-9_]{1,96}", value) is None for value in blockers):
            raise ValueError("Tomorrow research prerequisite blockers are invalid")
        if self.status == "ready" and blockers or self.status == "blocked" and not blockers:
            raise ValueError("Tomorrow research prerequisite status is inconsistent")
        if self.schema_version != "tomorrow_research_prerequisite_v1" or self.production_authority:
            raise ValueError("Tomorrow research prerequisite cannot authorize production")
        object.__setattr__(self, "blockers", blockers)
        object.__setattr__(self, "content_hash", self.prerequisite_hash)


class TomorrowResearchPrerequisitePort(Protocol):
    def inspect(self) -> TomorrowResearchPrerequisite: ...


@dataclass
class _AdvanceState:
    graph: TomorrowResearchArtifactGraph
    prerequisite: TomorrowResearchPrerequisite
    completed: list[TomorrowResearchStage]


@dataclass(frozen=True)
class TomorrowResearchAdvanceResult:
    status: Literal["blocked", "advanced", "artifact_conflict", "terminal"]
    graph_hash: str
    run_id: str | None
    completed_stages: tuple[TomorrowResearchStage, ...]
    next_stage: TomorrowResearchStage | None
    blockers: tuple[str, ...]
    readiness: TomorrowProductionReadinessAudit
    prerequisite_hash: str
    schema_version: str = "tomorrow_research_advance_result_v1"
    production_authority: bool = False
    automatic_model_update: bool = False
    content_hash: str = dataclasses.field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != "tomorrow_research_advance_result_v1":
            raise ValueError("Tomorrow research advance result schema is invalid")
        if _SHA256.fullmatch(self.prerequisite_hash) is None:
            raise ValueError("Tomorrow research prerequisite hash is invalid")
        if self.production_authority or self.automatic_model_update:
            raise ValueError("Tomorrow research orchestration cannot authorize production")
        object.__setattr__(self, "blockers", tuple(sorted(set(self.blockers))))
        object.__setattr__(self, "content_hash", canonical_hash(self))


class TomorrowResearchOrchestrator:
    def __init__(
        self,
        store: TomorrowResearchOrchestrationStore,
        prerequisite: TomorrowResearchPrerequisitePort,
        progress: TomorrowResearchProgressPort | None = None,
    ) -> None:
        self._store = store
        self._prerequisite = prerequisite
        self._progress = progress

    def advance(self) -> TomorrowResearchAdvanceResult:
        prerequisite = self._prerequisite.inspect()
        state = _AdvanceState(self._store.load_graph(), prerequisite, [])
        while True:
            stage = next_research_stage(state.graph)
            if stage is None:
                terminal_status: Literal["advanced", "terminal"] = "advanced" if state.completed else "terminal"
                return self._result(terminal_status, state, None, ())
            if prerequisite.status == "blocked":
                self._update(stage, "blocked")
                return self._result("blocked", state, stage, prerequisite.blockers)
            handoff = self._store.load_handoff(stage)
            if handoff is None:
                self._update(stage, "blocked")
                blocked_status: Literal["advanced", "blocked"] = "advanced" if state.completed else "blocked"
                return self._result(blocked_status, state, stage, (f"{stage}_handoff_missing",))
            expected_parent = None if not state.graph.artifacts else state.graph.content_hash
            if handoff.parent_graph_hash != expected_parent:
                self._update(stage, "blocked")
                return self._result("artifact_conflict", state, stage, (f"{stage}_parent_graph_mismatch",))
            self._update(stage, "sealing")
            state.graph = self._store.commit(state.graph.content_hash, handoff)
            state.completed.append(stage)
            self._update(stage, "completed")
            if handoff.outcome in {"historical_data_insufficient", "historical_rejected"}:
                return self._result("advanced", state, None, handoff.failure_reasons)

    def _update(
        self,
        stage: TomorrowResearchStage,
        status: Literal["blocked", "sealing", "completed"],
    ) -> None:
        if self._progress is not None:
            self._progress.update(stage, status)

    @staticmethod
    def _result(
        status: Literal["blocked", "advanced", "artifact_conflict", "terminal"],
        state: _AdvanceState,
        next_stage: TomorrowResearchStage | None,
        blockers: tuple[str, ...],
    ) -> TomorrowResearchAdvanceResult:
        return TomorrowResearchAdvanceResult(
            status=status,
            graph_hash=state.graph.content_hash,
            run_id=derive_tomorrow_research_run_id(state.graph),
            completed_stages=tuple(state.completed),
            next_stage=next_stage,
            blockers=blockers,
            readiness=production_readiness_audit(state.graph, manual_authorization_hash=None),
            prerequisite_hash=state.prerequisite.prerequisite_hash,
        )


__all__ = [
    "TomorrowResearchAdvanceResult",
    "TomorrowResearchOrchestrationStore",
    "TomorrowResearchOrchestrator",
    "TomorrowResearchPrerequisite",
    "TomorrowResearchPrerequisitePort",
    "TomorrowResearchProgressPort",
]
