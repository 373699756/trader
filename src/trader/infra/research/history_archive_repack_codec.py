"""JSON persistence boundary for crash-safe history archive repacking."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import cast

from trader.application.research.tomorrow_training import TomorrowTrainingStage
from trader.infra.research.history_archive_repack_state import (
    HistoryArchiveRepackActivationJournal,
    HistoryArchiveRepackActivationState,
    HistoryArchiveRepackBuildState,
    HistoryArchiveRepackPartitionEvidence,
    HistoryArchiveRepackSourceFileIdentity,
)
from trader.infra.scoring.artifact_hashing import artifact_content_hash
from trader.infra.scoring.profiles.v3.training_memory_evidence import (
    TomorrowTrainingMemoryEvidence,
    TomorrowTrainingStageDuration,
)


class HistoryArchiveRepackCodecError(RuntimeError):
    """A persisted repack state document is missing or untrusted."""


def write_history_archive_repack_build_state(path: Path, state: HistoryArchiveRepackBuildState) -> None:
    partitions = [
        {
            "relative_path": item.relative_path,
            "source_sha256": item.source_sha256,
            "target_sha256": item.target_sha256,
            "row_count": item.row_count,
            "observation_count": item.observation_count,
            "latest_row_count": item.latest_row_count,
            "source_bytes": item.source_bytes,
            "target_bytes": item.target_bytes,
            "records_logical_hash": item.records_logical_hash,
            "observations_logical_hash": item.observations_logical_hash,
        }
        for item in state.partitions
    ]
    source_files = [
        {
            "relative_path": item.relative_path,
            "size_bytes": item.size_bytes,
            "modified_ns": item.modified_ns,
        }
        for item in state.source_files
    ]
    payload: dict[str, object] = {
        "schema_version": "history_archive_repack_build_state",
        "source_root": state.source_root,
        "target_root": state.target_root,
        "source_snapshot_hash": state.source_snapshot_hash,
        "source_sequence": state.source_sequence,
        "source_file_identity_hash": state.source_file_identity_hash,
        "source_files": source_files,
        "source_bytes": state.source_bytes,
        "security_count": state.security_count,
        "trading_day_count": state.trading_day_count,
        "expected_partition_count": state.expected_partition_count,
        "page_size": state.page_size,
        "partitions": partitions,
        "target_snapshot_hash": state.target_snapshot_hash,
        "completed": state.completed,
    }
    _write_verified_json(path, payload)


def read_history_archive_repack_build_state(path: Path) -> HistoryArchiveRepackBuildState:
    payload = _read_verified_json(path, "history_archive_repack_build_state")
    _require_fields(
        payload,
        {
            "schema_version",
            "source_root",
            "target_root",
            "source_snapshot_hash",
            "source_sequence",
            "source_file_identity_hash",
            "source_files",
            "source_bytes",
            "security_count",
            "trading_day_count",
            "expected_partition_count",
            "page_size",
            "partitions",
            "target_snapshot_hash",
            "completed",
            "content_hash",
        },
    )
    try:
        raw_files = _list(payload, "source_files")
        source_files = tuple(_decode_source_file(item) for item in raw_files)
        raw_partitions = _list(payload, "partitions")
        partitions = tuple(_decode_partition(item) for item in raw_partitions)
        target_hash = payload["target_snapshot_hash"]
        if target_hash is not None and not isinstance(target_hash, str):
            raise TypeError("target snapshot hash must be optional text")
        completed = payload["completed"]
        if not isinstance(completed, bool):
            raise TypeError("completed must be boolean")
        return HistoryArchiveRepackBuildState(
            _text(payload, "source_root"),
            _text(payload, "target_root"),
            _text(payload, "source_snapshot_hash"),
            _integer(payload, "source_sequence"),
            _text(payload, "source_file_identity_hash"),
            source_files,
            _integer(payload, "source_bytes"),
            _integer(payload, "security_count"),
            _integer(payload, "trading_day_count"),
            _integer(payload, "expected_partition_count"),
            _integer(payload, "page_size"),
            partitions,
            target_hash,
            completed,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise HistoryArchiveRepackCodecError("history repack build state is invalid") from exc


def write_history_archive_repack_activation_journal(path: Path, journal: HistoryArchiveRepackActivationJournal) -> None:
    payload: dict[str, object] = {
        "schema_version": "history_archive_repack_activation_journal",
        "state": journal.state,
        "source_root": journal.source_root,
        "target_root": journal.target_root,
        "backup_root": journal.backup_root,
        "source_snapshot_hash": journal.source_snapshot_hash,
        "target_snapshot_hash": journal.target_snapshot_hash,
    }
    _write_verified_json(path, payload)


def read_history_archive_repack_activation_journal(path: Path) -> HistoryArchiveRepackActivationJournal:
    payload = _read_verified_json(path, "history_archive_repack_activation_journal")
    _require_fields(
        payload,
        {
            "schema_version",
            "state",
            "source_root",
            "target_root",
            "backup_root",
            "source_snapshot_hash",
            "target_snapshot_hash",
            "content_hash",
        },
    )
    try:
        return HistoryArchiveRepackActivationJournal(
            cast(HistoryArchiveRepackActivationState, _text(payload, "state")),
            _text(payload, "source_root"),
            _text(payload, "target_root"),
            _text(payload, "backup_root"),
            _text(payload, "source_snapshot_hash"),
            _text(payload, "target_snapshot_hash"),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise HistoryArchiveRepackCodecError("history repack activation journal is invalid") from exc


def read_tomorrow_training_memory_evidence(path: Path) -> TomorrowTrainingMemoryEvidence:
    payload = _read_verified_json(path, "tomorrow_training_memory_gate")
    _require_fields(
        payload,
        {
            "schema_version",
            "status",
            "training_status",
            "repeat_training_status",
            "training_input_hash",
            "model_hash",
            "report_hash",
            "peak_rss_bytes",
            "starting_peak_rss_bytes",
            "max_rss_bytes",
            "sample_database_peak_bytes",
            "stage_durations_ms",
            "failure_reasons",
            "content_hash",
        },
    )
    try:
        if _text(payload, "status") != "passed":
            raise ValueError("training memory gate did not pass")
        failures = _list(payload, "failure_reasons")
        if failures:
            raise ValueError("training memory gate has failure reasons")
        starting_peak_rss_bytes = _integer(payload, "starting_peak_rss_bytes")
        peak_rss_bytes = _integer(payload, "peak_rss_bytes")
        if starting_peak_rss_bytes < 1 or starting_peak_rss_bytes > peak_rss_bytes:
            raise ValueError("training memory starting peak is invalid")
        return TomorrowTrainingMemoryEvidence(
            _text(payload, "training_status"),
            _text(payload, "repeat_training_status"),
            _text(payload, "training_input_hash"),
            _text(payload, "model_hash"),
            _text(payload, "report_hash"),
            peak_rss_bytes,
            _integer(payload, "max_rss_bytes"),
            _integer(payload, "sample_database_peak_bytes"),
            _stage_durations(payload),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise HistoryArchiveRepackCodecError("Tomorrow training memory evidence is invalid") from exc


def _decode_source_file(raw: object) -> HistoryArchiveRepackSourceFileIdentity:
    payload = _object(raw)
    _require_fields(payload, {"relative_path", "size_bytes", "modified_ns"})
    return HistoryArchiveRepackSourceFileIdentity(
        _text(payload, "relative_path"),
        _integer(payload, "size_bytes"),
        _integer(payload, "modified_ns"),
    )


def _decode_partition(raw: object) -> HistoryArchiveRepackPartitionEvidence:
    payload = _object(raw)
    _require_fields(
        payload,
        {
            "relative_path",
            "source_sha256",
            "target_sha256",
            "row_count",
            "observation_count",
            "latest_row_count",
            "source_bytes",
            "target_bytes",
            "records_logical_hash",
            "observations_logical_hash",
        },
    )
    return HistoryArchiveRepackPartitionEvidence(
        _text(payload, "relative_path"),
        _text(payload, "source_sha256"),
        _text(payload, "target_sha256"),
        _integer(payload, "row_count"),
        _integer(payload, "observation_count"),
        _integer(payload, "latest_row_count"),
        _integer(payload, "source_bytes"),
        _integer(payload, "target_bytes"),
        _text(payload, "records_logical_hash"),
        _text(payload, "observations_logical_hash"),
    )


def _read_verified_json(path: Path, schema: str) -> dict[str, object]:
    try:
        payload = _object(json.loads(path.read_text(encoding="utf-8")))
        declared = payload.get("content_hash")
        body = {key: value for key, value in payload.items() if key != "content_hash"}
        if payload.get("schema_version") != schema or not isinstance(declared, str):
            raise ValueError("schema or hash is invalid")
        if artifact_content_hash(body) != declared:
            raise ValueError("content hash is invalid")
        return payload
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HistoryArchiveRepackCodecError(f"history repack document is invalid: {path.name}") from exc


def _write_verified_json(path: Path, payload: dict[str, object]) -> None:
    document = dict(payload)
    document["content_hash"] = artifact_content_hash(document)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(document, handle, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _object(raw: object) -> dict[str, object]:
    if not isinstance(raw, dict) or any(not isinstance(key, str) for key in raw):
        raise TypeError("history repack document must be an object")
    return cast(dict[str, object], raw)


def _list(payload: dict[str, object], key: str) -> list[object]:
    value = payload[key]
    if not isinstance(value, list):
        raise TypeError(f"history repack {key} must be a list")
    return cast(list[object], value)


def _text(payload: dict[str, object], key: str) -> str:
    value = payload[key]
    if not isinstance(value, str) or not value:
        raise TypeError(f"history repack {key} must be text")
    return value


def _integer(payload: dict[str, object], key: str) -> int:
    value = payload[key]
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"history repack {key} must be integer")
    return value


def _stage_durations(payload: dict[str, object]) -> tuple[TomorrowTrainingStageDuration, ...]:
    durations = _object(payload["stage_durations_ms"])
    decoded: list[TomorrowTrainingStageDuration] = []
    for stage, value in durations.items():
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise TypeError("history repack stage duration must be numeric")
        decoded.append(TomorrowTrainingStageDuration(cast(TomorrowTrainingStage, stage), float(value)))
    return tuple(sorted(decoded, key=lambda item: item.stage))


def _require_fields(payload: dict[str, object], fields: set[str]) -> None:
    if set(payload) != fields:
        raise ValueError("history repack document fields are invalid")


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = [
    "HistoryArchiveRepackCodecError",
    "read_history_archive_repack_activation_journal",
    "read_history_archive_repack_build_state",
    "read_tomorrow_training_memory_evidence",
    "write_history_archive_repack_activation_journal",
    "write_history_archive_repack_build_state",
]
