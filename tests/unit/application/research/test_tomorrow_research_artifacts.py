from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import date

import pytest

from trader.application.research.tomorrow_research_artifacts import (
    TomorrowResearchArtifactGraph,
    TomorrowResearchArtifactRef,
    TomorrowResearchEvidencePartitionRef,
    TomorrowResearchResourceProbe,
    TomorrowResearchStageHandoff,
    production_readiness_audit,
)


def _ref(artifact_id: str, owner: str, *, parents: tuple[str, ...] = ()) -> TomorrowResearchArtifactRef:
    return TomorrowResearchArtifactRef(
        artifact_id=artifact_id,
        artifact_kind=f"{artifact_id}_v1",
        owner=owner,  # type: ignore[arg-type]
        content_hash=hashlib.sha256(artifact_id.encode("ascii")).hexdigest(),
        parent_hashes=parents,
    )


def _probe(**overrides: object) -> TomorrowResearchResourceProbe:
    values: dict[str, object] = {
        "pilot_stocks": 100,
        "pilot_trade_dates": 120,
        "cpu_threads": 2,
        "peak_rss_mb": 1024,
        "available_disk_gb": 40.0,
        "estimated_full_run_hours": 8.0,
    }
    values.update(overrides)
    return TomorrowResearchResourceProbe(**values)  # type: ignore[arg-type]


def test_graph_is_canonical_and_rejects_missing_or_conflicting_parents() -> None:
    parent = _ref("h1_coverage_audit", "codex_a")
    child = _ref("daily_close_c3_candidate", "codex_a", parents=(parent.content_hash,))

    first = TomorrowResearchArtifactGraph((child, parent))
    second = TomorrowResearchArtifactGraph((parent, child))

    assert first == second
    assert first.content_hash == second.content_hash
    with pytest.raises(ValueError, match="parent"):
        TomorrowResearchArtifactGraph((child,))
    with pytest.raises(ValueError, match="identity"):
        TomorrowResearchArtifactGraph((parent, replace(parent, content_hash="f" * 64)))

    left = _ref("left", "codex_d", parents=("f" * 64,))
    right = replace(_ref("right", "codex_d"), content_hash="f" * 64, parent_hashes=(left.content_hash,))
    with pytest.raises(ValueError, match="cycle"):
        TomorrowResearchArtifactGraph((left, right))


def test_stage_handoff_binds_exact_required_roles_and_resource_limits() -> None:
    probe_ref = _ref("resource_probe_report", "codex_d")
    probe_handoff = TomorrowResearchStageHandoff(
        stage="resource_probe",
        parent_graph_hash=None,
        artifacts=(probe_ref,),
        resource_probe=_probe(),
    )
    artifacts = (
        _ref("h1_coverage_audit", "codex_a"),
        _ref("daily_close_c3_candidate", "codex_a"),
        _ref("filter_confirmation", "codex_b"),
        _ref("tomorrow_joint_candidate", "codex_b"),
    )
    handoff = TomorrowResearchStageHandoff(
        stage="development_training",
        parent_graph_hash=None,
        artifacts=artifacts,
    )

    assert handoff.production_authority is False
    assert handoff.automatic_model_update is False
    with pytest.raises(ValueError, match="required artifacts"):
        replace(handoff, artifacts=artifacts[:-1])
    with pytest.raises(ValueError, match="resource"):
        replace(probe_handoff, resource_probe=_probe(peak_rss_mb=4097))
    with pytest.raises(ValueError, match="owner"):
        replace(handoff, artifacts=(replace(artifacts[0], owner="codex_b"), *artifacts[1:]))


def test_production_readiness_requires_both_holdouts_parity_and_new_authorization() -> None:
    empty = TomorrowResearchArtifactGraph(())

    audit = production_readiness_audit(empty, manual_authorization_hash=None)

    assert audit.status == "production_adaptation_blocked"
    assert audit.blockers == (
        "daily_close_proxy_not_validated",
        "manual_production_authorization_missing",
        "point_in_time_holdout_not_validated",
    )
    assert audit.production_authority is False
    assert audit.automatic_model_update is False


def test_evidence_partition_rejects_absolute_or_traversing_paths() -> None:
    values = {
        "content_hash": "a" * 64,
        "schema_hash": "b" * 64,
        "row_count": 1,
        "first_trade_date": date(2024, 1, 2),
        "last_trade_date": date(2024, 1, 2),
    }

    with pytest.raises(ValueError, match="relative Parquet"):
        TomorrowResearchEvidencePartitionRef(relative_path="../outside.parquet", **values)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="relative Parquet"):
        TomorrowResearchEvidencePartitionRef(relative_path="/absolute.parquet", **values)  # type: ignore[arg-type]
