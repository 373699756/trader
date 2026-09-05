"""Typed contracts for the immutable Tomorrow research artifact graph."""

from __future__ import annotations

import dataclasses
import math
import re
from dataclasses import dataclass
from datetime import date
from pathlib import PurePosixPath
from typing import Literal

from trader.application.research.replay_models import canonical_hash

TomorrowResearchOwner = Literal["codex_a", "codex_b", "codex_c", "codex_d"]
TomorrowResearchStage = Literal[
    "resource_probe",
    "development_training",
    "confirmation",
    "daily_close_proxy_holdout",
    "point_in_time_holdout",
]
TomorrowResearchTerminalStatus = Literal[
    "historical_data_insufficient",
    "historical_rejected",
    "historical_daily_close_proxy_validated",
    "historical_validated",
]
TomorrowResearchHandoffOutcome = Literal[
    "stage_ready",
    "historical_data_insufficient",
    "historical_rejected",
    "historical_daily_close_proxy_validated",
    "historical_validated",
]

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_IDENTITY = re.compile(r"^[a-z0-9_]{1,96}$")
_STAGES: tuple[TomorrowResearchStage, ...] = (
    "resource_probe",
    "development_training",
    "confirmation",
    "daily_close_proxy_holdout",
    "point_in_time_holdout",
)
_REQUIRED_ARTIFACTS: dict[TomorrowResearchStage, frozenset[str]] = {
    "resource_probe": frozenset({"resource_probe_report"}),
    "development_training": frozenset(
        {
            "h1_coverage_audit",
            "daily_close_c3_candidate",
            "filter_confirmation",
            "tomorrow_joint_candidate",
        }
    ),
    "confirmation": frozenset({"daily_close_confirmation_report", "joint_confirmation_report"}),
    "daily_close_proxy_holdout": frozenset({"daily_close_proxy_validation_report", "joint_candidate_model_artifact"}),
    "point_in_time_holdout": frozenset({"tomorrow_point_in_time_holdout_report", "cross_strategy_conclusion"}),
}
_TERMINAL_REPORT_ARTIFACT: dict[TomorrowResearchStage, str] = {
    "resource_probe": "resource_probe_report",
    "development_training": "h1_coverage_audit",
    "confirmation": "daily_close_confirmation_report",
    "daily_close_proxy_holdout": "daily_close_proxy_validation_report",
    "point_in_time_holdout": "tomorrow_point_in_time_holdout_report",
}
_ARTIFACT_OWNERS: dict[str, TomorrowResearchOwner] = {
    "resource_probe_report": "codex_d",
    "h1_coverage_audit": "codex_a",
    "daily_close_c3_candidate": "codex_a",
    "filter_confirmation": "codex_b",
    "tomorrow_joint_candidate": "codex_b",
    "daily_close_confirmation_report": "codex_a",
    "joint_confirmation_report": "codex_b",
    "daily_close_proxy_validation_report": "codex_a",
    "joint_candidate_model_artifact": "codex_b",
    "tomorrow_point_in_time_holdout_report": "codex_c",
    "cross_strategy_conclusion": "codex_c",
}


@dataclass(frozen=True)
class TomorrowResearchArtifactRef:
    artifact_id: str
    artifact_kind: str
    owner: TomorrowResearchOwner
    content_hash: str
    parent_hashes: tuple[str, ...] = ()
    terminal_status: TomorrowResearchTerminalStatus | None = None
    evidence_markers: tuple[str, ...] = ()
    schema_version: str = "tomorrow_research_artifact_ref"
    production_authority: bool = False

    def __post_init__(self) -> None:
        if _IDENTITY.fullmatch(self.artifact_id) is None or _IDENTITY.fullmatch(self.artifact_kind) is None:
            raise ValueError("Tomorrow research artifact identity is invalid")
        if self.owner not in {"codex_a", "codex_b", "codex_c", "codex_d"}:
            raise ValueError("Tomorrow research artifact owner is invalid")
        _validate_hash(self.content_hash, "artifact content")
        parents = tuple(sorted(set(self.parent_hashes)))
        if len(parents) != len(self.parent_hashes) or any(_SHA256.fullmatch(value) is None for value in parents):
            raise ValueError("Tomorrow research artifact parent hashes are invalid")
        markers = tuple(sorted(set(self.evidence_markers)))
        if len(markers) != len(self.evidence_markers) or any(_IDENTITY.fullmatch(value) is None for value in markers):
            raise ValueError("Tomorrow research artifact evidence markers are invalid")
        if self.schema_version != "tomorrow_research_artifact_ref" or self.production_authority:
            raise ValueError("Tomorrow research artifact contract cannot authorize production")
        object.__setattr__(self, "parent_hashes", parents)
        object.__setattr__(self, "evidence_markers", markers)


@dataclass(frozen=True)
class TomorrowResearchArtifactGraph:
    artifacts: tuple[TomorrowResearchArtifactRef, ...]
    schema_version: str = "tomorrow_research_artifact_graph"
    production_authority: bool = False
    automatic_model_update: bool = False
    content_hash: str = dataclasses.field(init=False)

    def __post_init__(self) -> None:
        artifacts = tuple(sorted(self.artifacts, key=lambda item: item.artifact_id))
        artifact_ids = tuple(item.artifact_id for item in artifacts)
        hashes = tuple(item.content_hash for item in artifacts)
        if len(artifact_ids) != len(set(artifact_ids)) or len(hashes) != len(set(hashes)):
            raise ValueError("Tomorrow research artifact identity conflicts with an existing artifact")
        known_hashes = set(hashes)
        if any(parent not in known_hashes for item in artifacts for parent in item.parent_hashes):
            raise ValueError("Tomorrow research artifact parent is missing from the graph")
        if any(item.content_hash in item.parent_hashes for item in artifacts):
            raise ValueError("Tomorrow research artifact cannot reference itself as a parent")
        if _contains_cycle(artifacts):
            raise ValueError("Tomorrow research artifact graph contains a parent cycle")
        if self.schema_version != "tomorrow_research_artifact_graph":
            raise ValueError("Tomorrow research artifact graph schema is invalid")
        if self.production_authority or self.automatic_model_update:
            raise ValueError("Tomorrow research artifact graph cannot authorize production or automatic updates")
        object.__setattr__(self, "artifacts", artifacts)
        object.__setattr__(self, "content_hash", canonical_hash(self))

    def extend(self, artifacts: tuple[TomorrowResearchArtifactRef, ...]) -> TomorrowResearchArtifactGraph:
        return TomorrowResearchArtifactGraph((*self.artifacts, *artifacts))


@dataclass(frozen=True)
class TomorrowResearchResourceProbe:
    pilot_stocks: int
    pilot_trade_dates: int
    cpu_threads: int
    peak_rss_mb: int
    available_disk_gb: float
    estimated_full_run_hours: float
    schema_version: str = "tomorrow_research_resource_probe"

    def __post_init__(self) -> None:
        if min(self.pilot_stocks, self.pilot_trade_dates, self.cpu_threads, self.peak_rss_mb) < 1:
            raise ValueError("Tomorrow research resource measurements must be positive")
        if not math.isfinite(self.available_disk_gb) or not math.isfinite(self.estimated_full_run_hours):
            raise ValueError("Tomorrow research resource measurements must be finite")
        if self.available_disk_gb < 0.0 or self.estimated_full_run_hours <= 0.0:
            raise ValueError("Tomorrow research resource measurements are invalid")
        if self.schema_version != "tomorrow_research_resource_probe":
            raise ValueError("Tomorrow research resource probe schema is invalid")

    @property
    def blockers(self) -> tuple[str, ...]:
        blockers: list[str] = []
        if self.pilot_stocks != 100 or self.pilot_trade_dates != 120:
            blockers.append("resource_probe_sample_mismatch")
        if self.cpu_threads > 2:
            blockers.append("resource_probe_cpu_limit_exceeded")
        if self.peak_rss_mb > 4096:
            blockers.append("resource_probe_rss_limit_exceeded")
        if self.available_disk_gb < 30.0:
            blockers.append("resource_probe_disk_below_30gb")
        if self.estimated_full_run_hours > 18.0:
            blockers.append("resource_probe_estimate_above_18h")
        return tuple(blockers)


@dataclass(frozen=True)
class TomorrowResearchEvidencePartitionRef:
    relative_path: str
    content_hash: str
    schema_hash: str
    row_count: int
    first_trade_date: date
    last_trade_date: date
    file_format: Literal["parquet"] = "parquet"
    schema_version: str = "tomorrow_research_evidence_partition_ref"

    def __post_init__(self) -> None:
        path = PurePosixPath(self.relative_path)
        if (
            path.is_absolute()
            or not path.parts
            or any(part in {"", ".", ".."} for part in path.parts)
            or path.suffix != ".parquet"
        ):
            raise ValueError("Tomorrow research evidence path must be a relative Parquet path")
        _validate_hash(self.content_hash, "evidence content")
        _validate_hash(self.schema_hash, "evidence schema")
        if self.row_count < 1 or self.first_trade_date > self.last_trade_date:
            raise ValueError("Tomorrow research evidence partition range is invalid")
        if self.file_format != "parquet" or self.schema_version != "tomorrow_research_evidence_partition_ref":
            raise ValueError("Tomorrow research evidence partition contract is invalid")


@dataclass(frozen=True)
class TomorrowResearchStageHandoff:
    stage: TomorrowResearchStage
    parent_graph_hash: str | None
    artifacts: tuple[TomorrowResearchArtifactRef, ...]
    evidence_partitions: tuple[TomorrowResearchEvidencePartitionRef, ...] = ()
    resource_probe: TomorrowResearchResourceProbe | None = None
    outcome: TomorrowResearchHandoffOutcome = "stage_ready"
    failure_reasons: tuple[str, ...] = ()
    schema_version: str = "tomorrow_research_stage_handoff"
    production_authority: bool = False
    automatic_model_update: bool = False
    content_hash: str = dataclasses.field(init=False)

    def __post_init__(self) -> None:
        if self.stage not in _STAGES:
            raise ValueError("Tomorrow research stage is invalid")
        if self.parent_graph_hash is not None:
            _validate_hash(self.parent_graph_hash, "parent graph")
        artifacts = tuple(sorted(self.artifacts, key=lambda item: item.artifact_id))
        evidence = tuple(sorted(self.evidence_partitions, key=lambda item: item.relative_path))
        _validate_stage_artifacts(self.stage, self.outcome, artifacts)
        _validate_stage_evidence(self.stage, evidence)
        _validate_stage_resource(self.stage, self.outcome, self.resource_probe)
        _validate_handoff_outcome(self.stage, self.outcome, artifacts)
        reasons = tuple(sorted(set(self.failure_reasons)))
        _validate_stage_reasons(self.outcome, reasons)
        _validate_stage_identity(self)
        object.__setattr__(self, "artifacts", artifacts)
        object.__setattr__(self, "evidence_partitions", evidence)
        object.__setattr__(self, "failure_reasons", reasons)
        object.__setattr__(self, "content_hash", canonical_hash(self))


@dataclass(frozen=True)
class TomorrowProductionReadinessAudit:
    status: Literal["production_adaptation_blocked", "production_adaptation_eligible"]
    graph_hash: str
    blockers: tuple[str, ...]
    manual_authorization_hash: str | None
    schema_version: str = "tomorrow_production_readiness_audit"
    production_authority: bool = False
    automatic_model_update: bool = False
    content_hash: str = dataclasses.field(init=False)

    def __post_init__(self) -> None:
        _validate_hash(self.graph_hash, "readiness graph")
        if self.manual_authorization_hash is not None:
            _validate_hash(self.manual_authorization_hash, "manual authorization")
        blockers = tuple(sorted(set(self.blockers)))
        expected = "production_adaptation_blocked" if blockers else "production_adaptation_eligible"
        if self.status != expected:
            raise ValueError("Tomorrow production readiness status is inconsistent")
        if self.schema_version != "tomorrow_production_readiness_audit":
            raise ValueError("Tomorrow production readiness audit schema is invalid")
        if self.production_authority or self.automatic_model_update:
            raise ValueError("Tomorrow production readiness audit cannot grant production authority")
        object.__setattr__(self, "blockers", blockers)
        object.__setattr__(self, "content_hash", canonical_hash(self))


def next_research_stage(graph: TomorrowResearchArtifactGraph) -> TomorrowResearchStage | None:
    artifact_ids = {item.artifact_id for item in graph.artifacts}
    for stage in _STAGES:
        if _stage_has_negative_terminal(graph, stage):
            return None
        if not _REQUIRED_ARTIFACTS[stage].issubset(artifact_ids):
            return stage
    return None


def derive_tomorrow_research_run_id(graph: TomorrowResearchArtifactGraph) -> str | None:
    resource_probe = _artifact(graph, "resource_probe_report")
    if resource_probe is None:
        return None
    return canonical_hash(
        {
            "schema_version": "tomorrow_research_run_identity",
            "sealed_input_and_resource_probe_hash": resource_probe.content_hash,
        }
    )


def production_readiness_audit(
    graph: TomorrowResearchArtifactGraph,
    *,
    manual_authorization_hash: str | None,
) -> TomorrowProductionReadinessAudit:
    blockers: list[str] = []
    proxy = _artifact(graph, "daily_close_proxy_validation_report")
    if proxy is None or proxy.terminal_status != "historical_daily_close_proxy_validated":
        blockers.append("daily_close_proxy_not_validated")
    point_in_time = _artifact(graph, "tomorrow_point_in_time_holdout_report")
    if point_in_time is None or point_in_time.terminal_status != "historical_validated":
        blockers.append("point_in_time_holdout_not_validated")
    elif "historical_point_in_time_parity" not in point_in_time.evidence_markers:
        blockers.append("historical_point_in_time_parity_missing")
    if manual_authorization_hash is None:
        blockers.append("manual_production_authorization_missing")
    return TomorrowProductionReadinessAudit(
        status="production_adaptation_blocked" if blockers else "production_adaptation_eligible",
        graph_hash=graph.content_hash,
        blockers=tuple(blockers),
        manual_authorization_hash=manual_authorization_hash,
    )


def _artifact(graph: TomorrowResearchArtifactGraph, artifact_id: str) -> TomorrowResearchArtifactRef | None:
    return next((item for item in graph.artifacts if item.artifact_id == artifact_id), None)


def _stage_has_negative_terminal(graph: TomorrowResearchArtifactGraph, stage: TomorrowResearchStage) -> bool:
    return any(
        item.artifact_id in _REQUIRED_ARTIFACTS[stage]
        and item.terminal_status in {"historical_data_insufficient", "historical_rejected"}
        for item in graph.artifacts
    )


def _validate_handoff_outcome(
    stage: TomorrowResearchStage,
    outcome: TomorrowResearchHandoffOutcome,
    artifacts: tuple[TomorrowResearchArtifactRef, ...],
) -> None:
    allowed: dict[TomorrowResearchStage, frozenset[TomorrowResearchHandoffOutcome]] = {
        "resource_probe": frozenset({"stage_ready", "historical_data_insufficient"}),
        "development_training": frozenset({"stage_ready", "historical_data_insufficient", "historical_rejected"}),
        "confirmation": frozenset({"stage_ready", "historical_data_insufficient", "historical_rejected"}),
        "daily_close_proxy_holdout": frozenset(
            {"historical_data_insufficient", "historical_rejected", "historical_daily_close_proxy_validated"}
        ),
        "point_in_time_holdout": frozenset(
            {"historical_data_insufficient", "historical_rejected", "historical_validated"}
        ),
    }
    if outcome not in allowed[stage]:
        raise ValueError("Tomorrow research stage outcome is invalid for its stage")
    terminal_values = {item.terminal_status for item in artifacts if item.terminal_status is not None}
    if outcome == "stage_ready" and terminal_values:
        raise ValueError("Tomorrow research ready stage cannot contain a terminal artifact")
    if outcome != "stage_ready" and outcome not in terminal_values:
        raise ValueError("Tomorrow research terminal outcome must be bound to an artifact")
    if outcome == "historical_validated":
        report = next(item for item in artifacts if item.artifact_id == "tomorrow_point_in_time_holdout_report")
        if "historical_point_in_time_parity" not in report.evidence_markers:
            raise ValueError("Tomorrow point-in-time validation requires parity evidence")


def _validate_stage_artifacts(
    stage: TomorrowResearchStage,
    outcome: TomorrowResearchHandoffOutcome,
    artifacts: tuple[TomorrowResearchArtifactRef, ...],
) -> None:
    artifact_ids = frozenset(item.artifact_id for item in artifacts)
    if len(artifact_ids) != len(artifacts) or not artifact_ids.issubset(_REQUIRED_ARTIFACTS[stage]):
        raise ValueError("Tomorrow research stage required artifacts are incomplete or unexpected")
    if any(item.owner != _ARTIFACT_OWNERS[item.artifact_id] for item in artifacts):
        raise ValueError("Tomorrow research stage artifact owner is invalid")
    successful = outcome in {"stage_ready", "historical_daily_close_proxy_validated", "historical_validated"}
    if successful and artifact_ids != _REQUIRED_ARTIFACTS[stage]:
        raise ValueError("Tomorrow research stage required artifacts are incomplete or unexpected")
    if not successful and _TERMINAL_REPORT_ARTIFACT[stage] not in artifact_ids:
        raise ValueError("Tomorrow research terminal stage requires its typed report artifact")


def _validate_stage_evidence(
    stage: TomorrowResearchStage,
    evidence: tuple[TomorrowResearchEvidencePartitionRef, ...],
) -> None:
    if len({item.relative_path for item in evidence}) != len(evidence):
        raise ValueError("Tomorrow research evidence partition paths must be unique")
    if stage == "resource_probe" and evidence:
        raise ValueError("Tomorrow research resource probe cannot produce data partitions")


def _validate_stage_resource(
    stage: TomorrowResearchStage,
    outcome: TomorrowResearchHandoffOutcome,
    resource_probe: TomorrowResearchResourceProbe | None,
) -> None:
    if stage != "resource_probe":
        if resource_probe is not None:
            raise ValueError("Tomorrow research measurements belong only to the resource probe stage")
        return
    if resource_probe is None:
        raise ValueError("Tomorrow research resource probe stage requires measurements")
    if outcome == "stage_ready" and resource_probe.blockers:
        raise ValueError("Tomorrow research resource probe has unresolved blockers")
    if outcome != "stage_ready" and not resource_probe.blockers:
        raise ValueError("Tomorrow research failed resource probe requires a measured blocker")


def _validate_stage_reasons(
    outcome: TomorrowResearchHandoffOutcome,
    reasons: tuple[str, ...],
) -> None:
    if any(_IDENTITY.fullmatch(value) is None for value in reasons):
        raise ValueError("Tomorrow research stage failure reasons are invalid")
    successful = outcome in {"stage_ready", "historical_daily_close_proxy_validated", "historical_validated"}
    if successful == bool(reasons):
        raise ValueError("Tomorrow research stage outcome and failure reasons are inconsistent")


def _validate_stage_identity(handoff: TomorrowResearchStageHandoff) -> None:
    if handoff.schema_version != "tomorrow_research_stage_handoff":
        raise ValueError("Tomorrow research stage handoff schema is invalid")
    if handoff.production_authority or handoff.automatic_model_update:
        raise ValueError("Tomorrow research stage cannot authorize production or automatic updates")


def _validate_hash(value: str, name: str) -> None:
    if _SHA256.fullmatch(value) is None:
        raise ValueError(f"Tomorrow research {name} hash is invalid")


def _contains_cycle(artifacts: tuple[TomorrowResearchArtifactRef, ...]) -> bool:
    parents_by_hash = {item.content_hash: item.parent_hashes for item in artifacts}
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(content_hash: str) -> bool:
        if content_hash in visiting:
            return True
        if content_hash in visited:
            return False
        visiting.add(content_hash)
        if any(visit(parent) for parent in parents_by_hash[content_hash]):
            return True
        visiting.remove(content_hash)
        visited.add(content_hash)
        return False

    return any(visit(content_hash) for content_hash in parents_by_hash)


__all__ = [
    "TomorrowProductionReadinessAudit",
    "TomorrowResearchArtifactGraph",
    "TomorrowResearchArtifactRef",
    "TomorrowResearchEvidencePartitionRef",
    "TomorrowResearchHandoffOutcome",
    "TomorrowResearchOwner",
    "TomorrowResearchResourceProbe",
    "TomorrowResearchStage",
    "TomorrowResearchStageHandoff",
    "TomorrowResearchTerminalStatus",
    "derive_tomorrow_research_run_id",
    "next_research_stage",
    "production_readiness_audit",
]
