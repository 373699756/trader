from __future__ import annotations

import hashlib
import json
from datetime import date

import pytest

from trader.application.research.replay_models import canonical_hash, canonical_json
from trader.application.research.research_tomorrow_orchestrator import (
    TomorrowResearchOrchestrator,
    TomorrowResearchPrerequisite,
)
from trader.application.research.tomorrow_research_artifacts import (
    TomorrowResearchArtifactGraph,
    TomorrowResearchArtifactRef,
    TomorrowResearchEvidencePartitionRef,
    TomorrowResearchResourceProbe,
    TomorrowResearchStageHandoff,
)
from trader.infra.research.tomorrow_research_artifacts import (
    TomorrowResearchArtifactStore,
    TomorrowResearchArtifactStoreError,
)


def _handoff() -> TomorrowResearchStageHandoff:
    return TomorrowResearchStageHandoff(
        stage="resource_probe",
        parent_graph_hash=None,
        artifacts=(TomorrowResearchArtifactRef("resource_probe_report", "resource_probe", "codex_d", "a" * 64),),
        resource_probe=TomorrowResearchResourceProbe(100, 120, 2, 1024, 40.0, 8.0),
    )


class _ReadyPrerequisite:
    def inspect(self) -> TomorrowResearchPrerequisite:
        return TomorrowResearchPrerequisite("ready", "9" * 64, ())


def _development_handoff(
    graph: TomorrowResearchArtifactGraph,
    evidence: tuple[TomorrowResearchEvidencePartitionRef, ...] = (),
) -> TomorrowResearchStageHandoff:
    return TomorrowResearchStageHandoff(
        stage="development_training",
        parent_graph_hash=graph.content_hash,
        artifacts=(
            TomorrowResearchArtifactRef("h1_coverage_audit", "h1_coverage_audit", "codex_a", "b" * 64, ("a" * 64,)),
            TomorrowResearchArtifactRef(
                "daily_close_c3_candidate", "daily_close_c3_candidate", "codex_a", "c" * 64, ("b" * 64,)
            ),
            TomorrowResearchArtifactRef("filter_confirmation", "filter_confirmation", "codex_b", "d" * 64, ("a" * 64,)),
            TomorrowResearchArtifactRef(
                "tomorrow_joint_candidate", "tomorrow_joint_candidate", "codex_b", "e" * 64, ("c" * 64,)
            ),
        ),
        evidence_partitions=evidence,
    )


def test_store_seals_handoff_idempotently_and_recovers_one_stage_commit(tmp_path) -> None:
    store = TomorrowResearchArtifactStore(tmp_path, available_disk_gb=lambda _path: 40.0)
    handoff = _handoff()

    store.seal_handoff(handoff)
    store.seal_handoff(handoff)
    graph = store.commit(store.load_graph().content_hash, handoff)

    assert store.load_graph() == graph
    assert store.load_handoff("resource_probe") is None
    assert len(graph.artifacts) == 1
    assert store.current_run_id() is not None
    assert (tmp_path / store.current_run_id() / ".report-checkpoint.json").is_file()
    assert not (tmp_path / store.current_run_id() / "report.json").exists()


def test_store_rejects_different_content_for_the_same_handoff_identity_and_tampering(tmp_path) -> None:
    store = TomorrowResearchArtifactStore(tmp_path, available_disk_gb=lambda _path: 40.0)
    store.seal_handoff(_handoff())
    path = tmp_path / ".handoffs" / "resource_probe.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["parent_graph_hash"] = "f" * 64
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(TomorrowResearchArtifactStoreError, match="invalid"):
        store.load_handoff("resource_probe")


def test_single_invocation_continues_all_available_stages_and_seals_terminal_documents(tmp_path) -> None:
    store = TomorrowResearchArtifactStore(tmp_path, available_disk_gb=lambda _path: 40.0)
    resource_probe = _handoff()
    graph = TomorrowResearchArtifactGraph(resource_probe.artifacts)
    evidence_bytes = b"PAR1fixturePAR1"
    evidence_hash = hashlib.sha256(evidence_bytes).hexdigest()
    evidence = TomorrowResearchEvidencePartitionRef(
        relative_path="features/trade_date=2024-01-02/part-000.parquet",
        content_hash=evidence_hash,
        schema_hash="4" * 64,
        row_count=3,
        first_trade_date=date(2024, 1, 2),
        last_trade_date=date(2024, 1, 2),
    )
    evidence_source = tmp_path / "fixture.parquet"
    evidence_source.write_bytes(evidence_bytes)
    development = _development_handoff(graph, (evidence,))
    graph = graph.extend(development.artifacts)
    confirmation = TomorrowResearchStageHandoff(
        stage="confirmation",
        parent_graph_hash=graph.content_hash,
        artifacts=(
            TomorrowResearchArtifactRef(
                "daily_close_confirmation_report", "daily_close_confirmation_report", "codex_a", "f" * 64, ("c" * 64,)
            ),
            TomorrowResearchArtifactRef(
                "joint_confirmation_report", "joint_confirmation_report", "codex_b", "0" * 64, ("e" * 64,)
            ),
        ),
    )
    graph = graph.extend(confirmation.artifacts)
    model_payload = {"schema_version": "tomorrow_joint_candidate_model_artifact"}
    model_hash = canonical_hash(model_payload)
    model_payload["content_hash"] = model_hash
    proxy = TomorrowResearchStageHandoff(
        stage="daily_close_proxy_holdout",
        parent_graph_hash=graph.content_hash,
        artifacts=(
            TomorrowResearchArtifactRef(
                "daily_close_proxy_validation_report",
                "daily_close_proxy_validation_report",
                "codex_a",
                "1" * 64,
                ("f" * 64,),
                "historical_daily_close_proxy_validated",
            ),
            TomorrowResearchArtifactRef(
                "joint_candidate_model_artifact",
                "tomorrow_joint_candidate_model_artifact",
                "codex_b",
                model_hash,
                ("0" * 64,),
            ),
        ),
        outcome="historical_daily_close_proxy_validated",
    )
    graph = graph.extend(proxy.artifacts)
    point_in_time = TomorrowResearchStageHandoff(
        stage="point_in_time_holdout",
        parent_graph_hash=graph.content_hash,
        artifacts=(
            TomorrowResearchArtifactRef(
                "tomorrow_point_in_time_holdout_report",
                "point_in_time_holdout",
                "codex_c",
                "2" * 64,
                ("1" * 64, model_hash),
                "historical_validated",
                ("historical_point_in_time_parity",),
            ),
            TomorrowResearchArtifactRef(
                "cross_strategy_conclusion", "cross_strategy_conclusion", "codex_c", "3" * 64, ("2" * 64,)
            ),
        ),
        outcome="historical_validated",
    )
    for handoff in (resource_probe, development, confirmation, proxy, point_in_time):
        store.seal_handoff(handoff)
    store.seal_evidence_partition(evidence, evidence_source)
    store.seal_model(canonical_json(model_payload), model_hash)

    result = TomorrowResearchOrchestrator(store, _ReadyPrerequisite()).advance()

    assert result.status == "advanced"
    assert result.completed_stages == (
        "resource_probe",
        "development_training",
        "confirmation",
        "daily_close_proxy_holdout",
        "point_in_time_holdout",
    )
    assert result.next_stage is None
    assert result.run_id is not None
    run_root = tmp_path / result.run_id
    assert (run_root / "report.json").is_file()
    assert (run_root / "model.json").is_file()
    assert (run_root / "evidence" / evidence.relative_path).read_bytes() == evidence_bytes
    report = json.loads((run_root / "report.json").read_text(encoding="utf-8"))
    assert report["publishable"] is True
    assert report["evidence_partitions"][0]["content_hash"] == evidence_hash
    assert report["production_blockers"] == ["manual_production_authorization_missing"]

    next_probe = TomorrowResearchStageHandoff(
        stage="resource_probe",
        parent_graph_hash=None,
        artifacts=(TomorrowResearchArtifactRef("resource_probe_report", "resource_probe", "codex_d", "5" * 64),),
        resource_probe=TomorrowResearchResourceProbe(100, 120, 2, 900, 39.0, 7.0),
    )
    store.seal_handoff(next_probe)
    next_result = TomorrowResearchOrchestrator(store, _ReadyPrerequisite()).advance()

    assert next_result.run_id is not None and next_result.run_id != result.run_id
    assert next_result.completed_stages == ("resource_probe",)
    assert next_result.next_stage == "development_training"
    assert (run_root / "report.json").is_file()


def test_model_document_rejects_tampering_before_it_can_enter_a_run(tmp_path) -> None:
    store = TomorrowResearchArtifactStore(tmp_path, available_disk_gb=lambda _path: 40.0)
    payload = {"schema_version": "tomorrow_joint_candidate_model_artifact"}
    expected_hash = canonical_hash(payload)
    payload["content_hash"] = expected_hash
    payload["unexpected"] = True

    with pytest.raises(TomorrowResearchArtifactStoreError, match="hash"):
        store.seal_model(canonical_json(payload), expected_hash)


def test_store_stops_before_committing_when_host_disk_is_below_30gb(tmp_path) -> None:
    store = TomorrowResearchArtifactStore(tmp_path, available_disk_gb=lambda _path: 29.999)
    handoff = _handoff()
    store.seal_handoff(handoff)

    with pytest.raises(TomorrowResearchArtifactStoreError, match="below 30GB"):
        store.commit(store.load_graph().content_hash, handoff)

    assert store.current_run_id() is None
