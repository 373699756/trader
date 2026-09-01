"""Advance the immutable Tomorrow research graph by at most one stage."""

from __future__ import annotations

import dataclasses
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
class TomorrowResearchAdvanceResult:
    status: Literal["blocked", "advanced", "artifact_conflict", "terminal"]
    graph_hash: str
    run_id: str | None
    completed_stages: tuple[TomorrowResearchStage, ...]
    next_stage: TomorrowResearchStage | None
    blockers: tuple[str, ...]
    readiness: TomorrowProductionReadinessAudit
    schema_version: str = "tomorrow_research_advance_result_v1"
    production_authority: bool = False
    automatic_model_update: bool = False
    content_hash: str = dataclasses.field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != "tomorrow_research_advance_result_v1":
            raise ValueError("Tomorrow research advance result schema is invalid")
        if self.production_authority or self.automatic_model_update:
            raise ValueError("Tomorrow research orchestration cannot authorize production")
        object.__setattr__(self, "blockers", tuple(sorted(set(self.blockers))))
        object.__setattr__(self, "content_hash", canonical_hash(self))


class TomorrowResearchOrchestrator:
    def __init__(
        self,
        store: TomorrowResearchOrchestrationStore,
        progress: TomorrowResearchProgressPort | None = None,
    ) -> None:
        self._store = store
        self._progress = progress

    def advance(self) -> TomorrowResearchAdvanceResult:
        graph = self._store.load_graph()
        completed: list[TomorrowResearchStage] = []
        while True:
            stage = next_research_stage(graph)
            if stage is None:
                terminal_status: Literal["advanced", "terminal"] = "advanced" if completed else "terminal"
                return self._result(terminal_status, graph, tuple(completed), None, ())
            handoff = self._store.load_handoff(stage)
            if handoff is None:
                self._update(stage, "blocked")
                blocked_status: Literal["advanced", "blocked"] = "advanced" if completed else "blocked"
                return self._result(blocked_status, graph, tuple(completed), stage, (f"{stage}_handoff_missing",))
            expected_parent = None if not graph.artifacts else graph.content_hash
            if handoff.parent_graph_hash != expected_parent:
                self._update(stage, "blocked")
                return self._result(
                    "artifact_conflict",
                    graph,
                    tuple(completed),
                    stage,
                    (f"{stage}_parent_graph_mismatch",),
                )
            self._update(stage, "sealing")
            graph = self._store.commit(graph.content_hash, handoff)
            completed.append(stage)
            self._update(stage, "completed")
            if handoff.outcome in {"historical_data_insufficient", "historical_rejected"}:
                return self._result("advanced", graph, tuple(completed), None, handoff.failure_reasons)

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
        graph: TomorrowResearchArtifactGraph,
        completed_stages: tuple[TomorrowResearchStage, ...],
        next_stage: TomorrowResearchStage | None,
        blockers: tuple[str, ...],
    ) -> TomorrowResearchAdvanceResult:
        return TomorrowResearchAdvanceResult(
            status=status,
            graph_hash=graph.content_hash,
            run_id=derive_tomorrow_research_run_id(graph),
            completed_stages=completed_stages,
            next_stage=next_stage,
            blockers=blockers,
            readiness=production_readiness_audit(graph, manual_authorization_hash=None),
        )


__all__ = [
    "TomorrowResearchAdvanceResult",
    "TomorrowResearchOrchestrationStore",
    "TomorrowResearchOrchestrator",
    "TomorrowResearchProgressPort",
]
