"""Canonical JSON storage for the Tomorrow research orchestration graph."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal, cast

from trader.application.research.replay_models import canonical_hash, canonical_json, canonical_value
from trader.application.research.tomorrow_research_artifacts import (
    TomorrowResearchArtifactGraph,
    TomorrowResearchArtifactRef,
    TomorrowResearchEvidencePartitionRef,
    TomorrowResearchHandoffOutcome,
    TomorrowResearchOwner,
    TomorrowResearchResourceProbe,
    TomorrowResearchStage,
    TomorrowResearchStageHandoff,
    TomorrowResearchTerminalStatus,
    derive_tomorrow_research_run_id,
    next_research_stage,
    production_readiness_audit,
)
from trader.infra.process_lock import ProcessLock, ProcessLockError


class TomorrowResearchArtifactStoreError(RuntimeError):
    pass


@dataclass(frozen=True)
class _TomorrowCommitContext:
    graph: TomorrowResearchArtifactGraph
    run_id: str
    active_run_id: str | None
    run_root: Path
    next_stage: TomorrowResearchStage | None
    model_encoded: str | None
    resource_probe: TomorrowResearchResourceProbe | None


class TomorrowResearchArtifactStore:
    """Seal handoffs and advance one content-addressed graph with compare-and-set."""

    def __init__(self, root: Path, *, available_disk_gb: Callable[[Path], float] | None = None) -> None:
        self._root = root
        self._handoff_root = root / ".handoffs"
        self._model_inbox_root = root / ".models"
        self._evidence_inbox_root = root / ".evidence"
        self._active_run_path = root / ".active-run"
        self._lock_path = root / ".orchestrator.lock"
        self._available_disk_gb = available_disk_gb or _available_disk_gb

    def seal_handoff(self, handoff: TomorrowResearchStageHandoff) -> str:
        path = self._handoff_path(handoff.stage)
        encoded = _encode(handoff)
        if path.is_file():
            existing = self.load_handoff(handoff.stage)
            if existing != handoff:
                raise TomorrowResearchArtifactStoreError("Tomorrow research handoff identity conflict")
            return handoff.content_hash
        _seal_immutable(path, encoded)
        existing = self.load_handoff(handoff.stage)
        if existing != handoff:
            raise TomorrowResearchArtifactStoreError("Tomorrow research handoff identity conflict")
        return handoff.content_hash

    def load_handoff(self, stage: TomorrowResearchStage) -> TomorrowResearchStageHandoff | None:
        path = self._handoff_path(stage)
        if not path.is_file():
            return None
        try:
            return _decode_handoff(path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise TomorrowResearchArtifactStoreError("Tomorrow research handoff is invalid") from exc

    def load_graph(self) -> TomorrowResearchArtifactGraph:
        run_id = self.current_run_id()
        if run_id is None:
            return TomorrowResearchArtifactGraph(())
        graph = self._load_run_graph(run_id)
        if next_research_stage(graph) is None:
            pending = self.load_handoff("resource_probe")
            if pending is not None:
                pending_graph = TomorrowResearchArtifactGraph(pending.artifacts)
                if derive_tomorrow_research_run_id(pending_graph) != run_id:
                    return TomorrowResearchArtifactGraph(())
        return graph

    def _load_run_graph(self, run_id: str) -> TomorrowResearchArtifactGraph:
        current_path = self._run_root(run_id) / ".checkpoint.json"
        try:
            return _decode_graph(current_path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise TomorrowResearchArtifactStoreError("Tomorrow research graph is invalid") from exc

    def seal_model(self, encoded: str, expected_hash: str) -> str:
        verified = _verified_model_document(encoded, expected_hash)
        _seal_immutable(self._model_inbox_root / f"{expected_hash}.json", verified)
        return expected_hash

    def seal_evidence_partition(self, reference: TomorrowResearchEvidencePartitionRef, source: Path) -> str:
        if not source.is_file() or _file_hash(source) != reference.content_hash:
            raise TomorrowResearchArtifactStoreError("Tomorrow research evidence partition hash is invalid")
        target = self._evidence_inbox_root / f"{reference.content_hash}.parquet"
        _seal_file_copy(source, target, reference.content_hash)
        return reference.content_hash

    def commit(
        self,
        expected_graph_hash: str,
        handoff: TomorrowResearchStageHandoff,
    ) -> TomorrowResearchArtifactGraph:
        try:
            with ProcessLock(self._lock_path):
                context = self._prepare_commit(expected_graph_hash, handoff)
                evidence = self._seal_commit_evidence(context.run_root, handoff.evidence_partitions)
                self._publish_commit(context, handoff, evidence)
                self._handoff_path(handoff.stage).unlink(missing_ok=True)
                return context.graph
        except ProcessLockError as exc:
            raise TomorrowResearchArtifactStoreError("Tomorrow research orchestrator is already active") from exc

    def _prepare_commit(
        self,
        expected_graph_hash: str,
        handoff: TomorrowResearchStageHandoff,
    ) -> _TomorrowCommitContext:
        current = self.load_graph()
        if current.content_hash != expected_graph_hash:
            raise TomorrowResearchArtifactStoreError("Tomorrow research graph compare-and-set conflict")
        if self.load_handoff(handoff.stage) != handoff:
            raise TomorrowResearchArtifactStoreError("Tomorrow research sealed handoff changed before commit")
        if self.host_available_disk_gb() < 30.0:
            raise TomorrowResearchArtifactStoreError("Tomorrow research available disk is below 30GB")
        updated = current.extend(handoff.artifacts)
        run_id = derive_tomorrow_research_run_id(updated)
        if run_id is None:
            raise TomorrowResearchArtifactStoreError("Tomorrow research run identity cannot be derived")
        active_run_id = self.current_run_id()
        self._validate_active_run(active_run_id, run_id)
        run_root = self._run_root(run_id)
        next_stage = next_research_stage(updated)
        return _TomorrowCommitContext(
            updated,
            run_id,
            active_run_id,
            run_root,
            next_stage,
            self._terminal_model(updated, next_stage),
            handoff.resource_probe or _read_report_resource_probe(run_root / ".report-checkpoint.json"),
        )

    def _validate_active_run(self, active_run_id: str | None, run_id: str) -> None:
        if active_run_id is None or active_run_id == run_id:
            return
        if next_research_stage(self._load_run_graph(active_run_id)) is not None:
            raise TomorrowResearchArtifactStoreError("Tomorrow research active run identity conflict")

    def _terminal_model(
        self,
        graph: TomorrowResearchArtifactGraph,
        next_stage: TomorrowResearchStage | None,
    ) -> str | None:
        model_ref = next(
            (item for item in graph.artifacts if item.artifact_id == "joint_candidate_model_artifact"),
            None,
        )
        if next_stage is not None or model_ref is None:
            return None
        model_source = self._model_inbox_root / f"{model_ref.content_hash}.json"
        if not model_source.is_file():
            raise TomorrowResearchArtifactStoreError("Tomorrow research terminal model document is missing")
        return _verified_model_document(model_source.read_text(encoding="utf-8"), model_ref.content_hash)

    def _seal_commit_evidence(
        self,
        run_root: Path,
        additions: tuple[TomorrowResearchEvidencePartitionRef, ...],
    ) -> tuple[TomorrowResearchEvidencePartitionRef, ...]:
        evidence = _read_report_evidence(run_root / ".report-checkpoint.json")
        evidence_by_path = {item.relative_path: item for item in evidence}
        for reference in additions:
            existing = evidence_by_path.get(reference.relative_path)
            if existing is not None and existing != reference:
                raise TomorrowResearchArtifactStoreError("Tomorrow research evidence partition identity conflict")
            source = self._evidence_inbox_root / f"{reference.content_hash}.parquet"
            if not source.is_file() or _file_hash(source) != reference.content_hash:
                raise TomorrowResearchArtifactStoreError("Tomorrow research sealed evidence partition is missing")
            _seal_file_copy(source, run_root / "evidence" / reference.relative_path, reference.content_hash)
            evidence_by_path[reference.relative_path] = reference
        return tuple(sorted(evidence_by_path.values(), key=lambda item: item.relative_path))

    def _publish_commit(
        self,
        context: _TomorrowCommitContext,
        handoff: TomorrowResearchStageHandoff,
        evidence: tuple[TomorrowResearchEvidencePartitionRef, ...],
    ) -> None:
        encoded_graph = _encode(context.graph)
        _seal_immutable(context.run_root / ".graphs" / f"{context.graph.content_hash}.json", encoded_graph)
        _replace_file(context.run_root / ".checkpoint.json", encoded_graph)
        report = _report(context.graph, handoff, context.run_id, context.resource_probe, evidence)
        _replace_file(context.run_root / ".report-checkpoint.json", report)
        if context.next_stage is None:
            _replace_file(context.run_root / "report.json", report)
            if context.model_encoded is not None:
                _seal_immutable(context.run_root / "model.json", context.model_encoded)
        if context.active_run_id != context.run_id:
            _replace_file(self._active_run_path, f"{context.run_id}\n")

    def host_available_disk_gb(self) -> float:
        path = self._root
        while not path.exists() and path != path.parent:
            path = path.parent
        return round(self._available_disk_gb(path), 3)

    def current_run_id(self) -> str | None:
        if not self._active_run_path.is_file():
            return None
        try:
            run_id = self._active_run_path.read_text(encoding="ascii").strip()
        except OSError as exc:
            raise TomorrowResearchArtifactStoreError("Tomorrow research active run pointer is invalid") from exc
        if len(run_id) != 64 or any(value not in "0123456789abcdef" for value in run_id):
            raise TomorrowResearchArtifactStoreError("Tomorrow research active run pointer is invalid")
        return run_id

    def _handoff_path(self, stage: TomorrowResearchStage) -> Path:
        return self._handoff_root / f"{stage}.json"

    def _run_root(self, run_id: str) -> Path:
        return self._root / run_id


def _encode(value: TomorrowResearchArtifactGraph | TomorrowResearchStageHandoff) -> str:
    payload = canonical_value(value)
    if not isinstance(payload, dict):
        raise TypeError("Tomorrow research artifact must encode to an object")
    payload["content_hash"] = value.content_hash
    return canonical_json(payload)


def _available_disk_gb(path: Path) -> float:
    return shutil.disk_usage(path).free / (1024**3)


def _decode_graph(encoded: str) -> TomorrowResearchArtifactGraph:
    raw = _verified_object(encoded)
    _exact_fields(
        raw,
        {
            "artifacts",
            "schema_version",
            "production_authority",
            "automatic_model_update",
            "content_hash",
        },
    )
    graph = TomorrowResearchArtifactGraph(
        artifacts=tuple(_decode_ref(_object(item)) for item in _array(raw["artifacts"])),
        schema_version=_string(raw["schema_version"]),
        production_authority=_boolean(raw["production_authority"]),
        automatic_model_update=_boolean(raw["automatic_model_update"]),
    )
    _same_hash(raw, graph.content_hash)
    return graph


def _decode_handoff(encoded: str) -> TomorrowResearchStageHandoff:
    raw = _verified_object(encoded)
    _exact_fields(
        raw,
        {
            "stage",
            "parent_graph_hash",
            "artifacts",
            "evidence_partitions",
            "resource_probe",
            "outcome",
            "failure_reasons",
            "schema_version",
            "production_authority",
            "automatic_model_update",
            "content_hash",
        },
    )
    resource_raw = raw["resource_probe"]
    handoff = TomorrowResearchStageHandoff(
        stage=cast(TomorrowResearchStage, _string(raw["stage"])),
        parent_graph_hash=_optional_string(raw["parent_graph_hash"]),
        artifacts=tuple(_decode_ref(_object(item)) for item in _array(raw["artifacts"])),
        evidence_partitions=tuple(
            _decode_evidence_partition(_object(item)) for item in _array(raw["evidence_partitions"])
        ),
        resource_probe=None if resource_raw is None else _decode_resource_probe(_object(resource_raw)),
        outcome=cast(TomorrowResearchHandoffOutcome, _string(raw["outcome"])),
        failure_reasons=_strings(raw["failure_reasons"]),
        schema_version=_string(raw["schema_version"]),
        production_authority=_boolean(raw["production_authority"]),
        automatic_model_update=_boolean(raw["automatic_model_update"]),
    )
    _same_hash(raw, handoff.content_hash)
    return handoff


def _decode_ref(raw: dict[str, object]) -> TomorrowResearchArtifactRef:
    _exact_fields(
        raw,
        {
            "artifact_id",
            "artifact_kind",
            "owner",
            "content_hash",
            "parent_hashes",
            "terminal_status",
            "evidence_markers",
            "schema_version",
            "production_authority",
        },
    )
    status = _optional_string(raw["terminal_status"])
    return TomorrowResearchArtifactRef(
        artifact_id=_string(raw["artifact_id"]),
        artifact_kind=_string(raw["artifact_kind"]),
        owner=cast(TomorrowResearchOwner, _string(raw["owner"])),
        content_hash=_string(raw["content_hash"]),
        parent_hashes=_strings(raw["parent_hashes"]),
        terminal_status=cast(TomorrowResearchTerminalStatus | None, status),
        evidence_markers=_strings(raw["evidence_markers"]),
        schema_version=_string(raw["schema_version"]),
        production_authority=_boolean(raw["production_authority"]),
    )


def _decode_resource_probe(raw: dict[str, object]) -> TomorrowResearchResourceProbe:
    _exact_fields(
        raw,
        {
            "pilot_stocks",
            "pilot_trade_dates",
            "cpu_threads",
            "peak_rss_mb",
            "available_disk_gb",
            "estimated_full_run_hours",
            "schema_version",
        },
    )
    return TomorrowResearchResourceProbe(
        pilot_stocks=_integer(raw["pilot_stocks"]),
        pilot_trade_dates=_integer(raw["pilot_trade_dates"]),
        cpu_threads=_integer(raw["cpu_threads"]),
        peak_rss_mb=_integer(raw["peak_rss_mb"]),
        available_disk_gb=_number(raw["available_disk_gb"]),
        estimated_full_run_hours=_number(raw["estimated_full_run_hours"]),
        schema_version=_string(raw["schema_version"]),
    )


def _decode_evidence_partition(raw: dict[str, object]) -> TomorrowResearchEvidencePartitionRef:
    _exact_fields(
        raw,
        {
            "relative_path",
            "content_hash",
            "schema_hash",
            "row_count",
            "first_trade_date",
            "last_trade_date",
            "file_format",
            "schema_version",
        },
    )
    return TomorrowResearchEvidencePartitionRef(
        relative_path=_string(raw["relative_path"]),
        content_hash=_string(raw["content_hash"]),
        schema_hash=_string(raw["schema_hash"]),
        row_count=_integer(raw["row_count"]),
        first_trade_date=_date(raw["first_trade_date"]),
        last_trade_date=_date(raw["last_trade_date"]),
        file_format=cast(Literal["parquet"], _string(raw["file_format"])),
        schema_version=_string(raw["schema_version"]),
    )


def _report(
    graph: TomorrowResearchArtifactGraph,
    handoff: TomorrowResearchStageHandoff,
    run_id: str,
    resource_probe: TomorrowResearchResourceProbe | None,
    evidence: tuple[TomorrowResearchEvidencePartitionRef, ...],
) -> str:
    next_stage = next_research_stage(graph)
    readiness = production_readiness_audit(graph, manual_authorization_hash=None)
    publishable = readiness.blockers == ("manual_production_authorization_missing",)
    payload: dict[str, object] = {
        "schema_version": "tomorrow_research_report_v1",
        "run_id": run_id,
        "status": "terminal" if next_stage is None else "in_progress",
        "graph_hash": graph.content_hash,
        "artifact_hashes": {item.artifact_id: item.content_hash for item in graph.artifacts},
        "artifact_graph": [
            {
                "artifact_id": item.artifact_id,
                "artifact_kind": item.artifact_kind,
                "owner": item.owner,
                "content_hash": item.content_hash,
                "parent_hashes": list(item.parent_hashes),
                "terminal_status": item.terminal_status,
                "evidence_markers": list(item.evidence_markers),
            }
            for item in graph.artifacts
        ],
        "completed_stage": handoff.stage,
        "next_stage": next_stage,
        "resource_probe": canonical_value(resource_probe),
        "evidence_partitions": [
            {
                "relative_path": item.relative_path,
                "content_hash": item.content_hash,
                "schema_hash": item.schema_hash,
                "row_count": item.row_count,
                "first_trade_date": item.first_trade_date.isoformat(),
                "last_trade_date": item.last_trade_date.isoformat(),
                "file_format": item.file_format,
            }
            for item in evidence
        ],
        "failure_reasons": list(handoff.failure_reasons),
        "production_readiness": readiness.status,
        "production_blockers": list(readiness.blockers),
        "publishable": publishable,
        "production_authority": False,
        "automatic_model_update": False,
    }
    payload["content_hash"] = canonical_hash(payload)
    return canonical_json(payload)


def _read_report_resource_probe(path: Path) -> TomorrowResearchResourceProbe | None:
    if not path.is_file():
        return None
    try:
        raw = _verified_object(path.read_text(encoding="utf-8"))
        value = raw.get("resource_probe")
        return None if value is None else _decode_resource_probe(_object(value))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise TomorrowResearchArtifactStoreError("Tomorrow research report checkpoint is invalid") from exc


def _read_report_evidence(path: Path) -> tuple[TomorrowResearchEvidencePartitionRef, ...]:
    if not path.is_file():
        return ()
    try:
        raw = _verified_object(path.read_text(encoding="utf-8"))
        return tuple(_decode_report_evidence(_object(item)) for item in _array(raw.get("evidence_partitions", [])))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise TomorrowResearchArtifactStoreError("Tomorrow research report checkpoint is invalid") from exc


def _decode_report_evidence(raw: dict[str, object]) -> TomorrowResearchEvidencePartitionRef:
    _exact_fields(
        raw,
        {
            "relative_path",
            "content_hash",
            "schema_hash",
            "row_count",
            "first_trade_date",
            "last_trade_date",
            "file_format",
        },
    )
    return TomorrowResearchEvidencePartitionRef(
        relative_path=_string(raw["relative_path"]),
        content_hash=_string(raw["content_hash"]),
        schema_hash=_string(raw["schema_hash"]),
        row_count=_integer(raw["row_count"]),
        first_trade_date=_date(raw["first_trade_date"]),
        last_trade_date=_date(raw["last_trade_date"]),
        file_format=cast(Literal["parquet"], _string(raw["file_format"])),
    )


def _verified_object(encoded: str) -> dict[str, object]:
    raw = json.loads(encoded)
    if not isinstance(raw, dict) or any(not isinstance(key, str) for key in raw):
        raise TypeError("Tomorrow research artifact must be an object")
    payload = cast(dict[str, object], raw)
    stored_hash = payload.pop("content_hash")
    if not isinstance(stored_hash, str) or canonical_hash(payload) != stored_hash:
        raise ValueError("Tomorrow research artifact hash mismatch")
    payload["content_hash"] = stored_hash
    return payload


def _verified_model_document(encoded: str, expected_hash: str) -> str:
    raw = json.loads(encoded)
    if not isinstance(raw, dict) or any(not isinstance(key, str) for key in raw):
        raise TomorrowResearchArtifactStoreError("Tomorrow research model document is invalid")
    payload = cast(dict[str, object], raw)
    stored_hash = payload.pop("content_hash", None)
    if stored_hash != expected_hash or canonical_hash(payload) != expected_hash:
        raise TomorrowResearchArtifactStoreError("Tomorrow research model document hash is invalid")
    payload["content_hash"] = expected_hash
    return canonical_json(payload)


def _seal_immutable(path: Path, encoded: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(encoded, encoding="utf-8")
    try:
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.read_text(encoding="utf-8") != encoded:
                raise TomorrowResearchArtifactStoreError("Tomorrow research immutable artifact conflict") from None
    finally:
        temporary.unlink(missing_ok=True)


def _seal_file_copy(source: Path, target: Path, expected_hash: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_file():
        if _file_hash(target) != expected_hash:
            raise TomorrowResearchArtifactStoreError("Tomorrow research immutable evidence conflict")
        return
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    shutil.copyfile(source, temporary)
    try:
        if _file_hash(temporary) != expected_hash:
            raise TomorrowResearchArtifactStoreError("Tomorrow research copied evidence hash is invalid")
        try:
            os.link(temporary, target)
        except FileExistsError:
            if _file_hash(target) != expected_hash:
                raise TomorrowResearchArtifactStoreError("Tomorrow research immutable evidence conflict") from None
    finally:
        temporary.unlink(missing_ok=True)


def _replace_file(path: Path, encoded: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(encoded, encoding="utf-8")
    os.replace(temporary, path)


def _same_hash(raw: dict[str, object], expected: str) -> None:
    if raw["content_hash"] != expected:
        raise ValueError("Tomorrow research reconstructed hash mismatch")


def _exact_fields(raw: dict[str, object], fields: set[str]) -> None:
    if set(raw) != fields:
        raise ValueError("Tomorrow research artifact schema fields are invalid")


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise TypeError("Tomorrow research artifact value must be an object")
    return cast(dict[str, object], value)


def _array(value: object) -> list[object]:
    if not isinstance(value, list):
        raise TypeError("Tomorrow research artifact value must be an array")
    return value


def _string(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("Tomorrow research artifact value must be a string")
    return value


def _optional_string(value: object) -> str | None:
    return None if value is None else _string(value)


def _strings(value: object) -> tuple[str, ...]:
    return tuple(_string(item) for item in _array(value))


def _boolean(value: object) -> bool:
    if not isinstance(value, bool):
        raise TypeError("Tomorrow research artifact value must be a boolean")
    return value


def _integer(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError("Tomorrow research artifact value must be an integer")
    return value


def _number(value: object) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise TypeError("Tomorrow research artifact value must be numeric")
    return float(value)


def _date(value: object) -> date:
    try:
        return date.fromisoformat(_string(value))
    except ValueError as exc:
        raise ValueError("Tomorrow research artifact date is invalid") from exc


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


__all__ = ["TomorrowResearchArtifactStore", "TomorrowResearchArtifactStoreError"]
