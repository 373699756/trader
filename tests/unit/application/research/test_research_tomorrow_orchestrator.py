from __future__ import annotations

from dataclasses import dataclass

from trader.application.research.research_tomorrow_orchestrator import (
    TomorrowResearchOrchestrator,
    TomorrowResearchPrerequisite,
)
from trader.application.research.tomorrow_research_artifacts import (
    TomorrowResearchArtifactGraph,
    TomorrowResearchArtifactRef,
    TomorrowResearchResourceProbe,
    TomorrowResearchStageHandoff,
)


def _ref(artifact_id: str, owner: str, hash_char: str) -> TomorrowResearchArtifactRef:
    return TomorrowResearchArtifactRef(
        artifact_id=artifact_id,
        artifact_kind=f"{artifact_id}_v1",
        owner=owner,  # type: ignore[arg-type]
        content_hash=hash_char * 64,
    )


def _resource_handoff() -> TomorrowResearchStageHandoff:
    return TomorrowResearchStageHandoff(
        stage="resource_probe",
        parent_graph_hash=None,
        artifacts=(_ref("resource_probe_report", "codex_d", "a"),),
        resource_probe=TomorrowResearchResourceProbe(100, 120, 2, 1024, 40.0, 8.0),
    )


@dataclass
class MemoryStore:
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
    value: TomorrowResearchPrerequisite

    def inspect(self) -> TomorrowResearchPrerequisite:
        return self.value


def _ready_prerequisite() -> FixedPrerequisite:
    return FixedPrerequisite(TomorrowResearchPrerequisite("ready", "f" * 64, ()))


def test_missing_upstream_artifacts_block_without_mutating_research_state() -> None:
    store = MemoryStore()

    result = TomorrowResearchOrchestrator(store, _ready_prerequisite()).advance()

    assert result.status == "blocked"
    assert result.next_stage == "resource_probe"
    assert result.blockers == ("resource_probe_handoff_missing",)
    assert result.prerequisite_hash == "f" * 64
    assert store.graph == TomorrowResearchArtifactGraph(())


def test_a_prerequisite_blocks_before_resource_handoff_without_mutating_state() -> None:
    store = MemoryStore(handoff=_resource_handoff())
    prerequisite = FixedPrerequisite(
        TomorrowResearchPrerequisite(
            "blocked",
            "e" * 64,
            ("tomorrow_h1_historical_data_insufficient",),
        )
    )

    result = TomorrowResearchOrchestrator(store, prerequisite).advance()

    assert result.status == "blocked"
    assert result.next_stage == "resource_probe"
    assert result.blockers == ("tomorrow_h1_historical_data_insufficient",)
    assert result.prerequisite_hash == "e" * 64
    assert store.graph == TomorrowResearchArtifactGraph(())
    assert store.handoff == _resource_handoff()


def test_each_invocation_continues_until_the_next_required_handoff_is_missing() -> None:
    store = MemoryStore(handoff=_resource_handoff())

    orchestrator = TomorrowResearchOrchestrator(store, _ready_prerequisite())
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
    store = MemoryStore(handoff=_resource_handoff())
    store.graph = store.graph.extend((_ref("existing", "codex_d", "e"),))

    result = TomorrowResearchOrchestrator(store, _ready_prerequisite()).advance()

    assert result.status == "artifact_conflict"
    assert result.blockers == ("resource_probe_parent_graph_mismatch",)
    assert tuple(item.artifact_id for item in store.graph.artifacts) == ("existing",)
