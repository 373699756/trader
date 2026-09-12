from __future__ import annotations

from dataclasses import dataclass

from trader.application.research.tomorrow_research_artifacts import (
    TomorrowResearchArtifactGraph,
    TomorrowResearchArtifactRef,
    TomorrowResearchResourceProbe,
    TomorrowResearchStageHandoff,
)
from trader.application.research.tomorrow_research_orchestrator import (
    TomorrowResearchOrchestrator,
    TomorrowResearchPrerequisiteStatus,
)


def _ref(artifact_id: str, hash_char: str, *, artifact_kind: str | None = None) -> TomorrowResearchArtifactRef:
    return TomorrowResearchArtifactRef(
        artifact_id=artifact_id,
        artifact_kind=artifact_kind or artifact_id,
        content_hash=hash_char * 64,
    )


def _resource_handoff() -> TomorrowResearchStageHandoff:
    return TomorrowResearchStageHandoff(
        stage="resource_probe",
        parent_graph_hash=None,
        artifacts=(_ref("resource_probe_report", "a", artifact_kind="resource_probe"),),
        resource_probe=TomorrowResearchResourceProbe(100, 120, 2, 1024, 40.0, 8.0),
    )


@dataclass
class MemoryOrchestrationRepository:
    graph: TomorrowResearchArtifactGraph = TomorrowResearchArtifactGraph(())
    handoff: TomorrowResearchStageHandoff | None = None

    def load_graph(self) -> TomorrowResearchArtifactGraph:
        return self.graph

    def load_handoff(self, stage: str) -> TomorrowResearchStageHandoff | None:
        del stage
        return self.handoff

    def commit(self, expected_graph_hash: str, handoff: TomorrowResearchStageHandoff) -> TomorrowResearchArtifactGraph:
        assert self.graph.content_hash == expected_graph_hash
        self.graph = self.graph.extend(handoff.artifacts)
        self.handoff = None
        return self.graph


@dataclass(frozen=True)
class FixedPrerequisite:
    value: TomorrowResearchPrerequisiteStatus

    def inspect(self) -> TomorrowResearchPrerequisiteStatus:
        return self.value


def _ready_prerequisite() -> FixedPrerequisite:
    return FixedPrerequisite(TomorrowResearchPrerequisiteStatus("ready", "f" * 64, ()))


def test_missing_upstream_artifacts_block_without_mutating_research_state() -> None:
    repository = MemoryOrchestrationRepository()

    result = TomorrowResearchOrchestrator(repository, _ready_prerequisite()).advance()

    assert result.status == "blocked"
    assert result.next_stage == "resource_probe"
    assert result.blockers == ("resource_probe_handoff_missing",)
    assert result.prerequisite_hash == "f" * 64
    assert repository.graph == TomorrowResearchArtifactGraph(())


def test_a_prerequisite_blocks_before_resource_handoff_without_mutating_state() -> None:
    repository = MemoryOrchestrationRepository(handoff=_resource_handoff())
    prerequisite = FixedPrerequisite(
        TomorrowResearchPrerequisiteStatus(
            "blocked",
            "e" * 64,
            ("tomorrow_h1_historical_data_insufficient",),
        )
    )

    result = TomorrowResearchOrchestrator(repository, prerequisite).advance()

    assert result.status == "blocked"
    assert result.next_stage == "resource_probe"
    assert result.blockers == ("tomorrow_h1_historical_data_insufficient",)
    assert result.prerequisite_hash == "e" * 64
    assert repository.graph == TomorrowResearchArtifactGraph(())
    assert repository.handoff == _resource_handoff()


def test_each_invocation_continues_until_the_next_required_handoff_is_missing() -> None:
    repository = MemoryOrchestrationRepository(handoff=_resource_handoff())

    orchestrator = TomorrowResearchOrchestrator(repository, _ready_prerequisite())
    first = orchestrator.advance()
    second = orchestrator.advance()

    assert first.status == "advanced"
    assert first.completed_stages == ("resource_probe",)
    assert first.run_id is not None
    assert first.next_stage == "development_training"
    assert second.status == "blocked"
    assert second.completed_stages == ()
    assert second.next_stage == "development_training"
    assert second.blockers == ("development_training_handoff_missing",)


def test_mismatched_parent_graph_fails_closed_without_importing_handoff() -> None:
    repository = MemoryOrchestrationRepository(handoff=_resource_handoff())
    repository.graph = repository.graph.extend((_ref("existing", "e"),))

    result = TomorrowResearchOrchestrator(repository, _ready_prerequisite()).advance()

    assert result.status == "artifact_conflict"
    assert result.blockers == ("resource_probe_parent_graph_mismatch",)
    assert tuple(item.artifact_id for item in repository.graph.artifacts) == ("existing",)
