"""Verify model and memory evidence before retiring a repack backup."""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

from trader.download.domain.history_control import HistoryRepackTrainingProof
from trader.infra.serialization.canonical import ContentHash
from trader.infra.serialization.fields import AsSequence
from trader.training.application.tomorrow_training import TomorrowTrainingStage
from trader.training.infra.memory_evidence import TomorrowTrainingMemoryEvidence, TomorrowTrainingStageDuration
from trader.training.infra.model_bundles.profile_publication import InspectActiveProfileBundles
from trader.training.infra.profile.v3.contracts import V3_TRAINING_PROFILE


class TrainingRepackEvidenceError(ValueError):
    """Training evidence is corrupt, incomplete or bound to other prices."""


def VerifyHistoryRepackTraining(
    training_root: Path, memory_evidence_path: Path, snapshot_hash: str
) -> HistoryRepackTrainingProof:
    bundle = InspectActiveProfileBundles(training_root.resolve() / "v3", V3_TRAINING_PROFILE)[0]
    evidence = ReadTomorrowTrainingMemoryEvidence(memory_evidence_path.resolve())
    if (
        bundle.training_input_hash != snapshot_hash
        or evidence.training_input_hash != snapshot_hash
        or evidence.model_hash != bundle.model_hash
        or evidence.report_hash != bundle.report_hash
    ):
        raise TrainingRepackEvidenceError("training evidence does not match repacked prices")
    return HistoryRepackTrainingProof(snapshot_hash, bundle.model_hash, bundle.report_hash)


def ReadTomorrowTrainingMemoryEvidence(path: Path) -> TomorrowTrainingMemoryEvidence:
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
        raise TrainingRepackEvidenceError("Tomorrow training memory evidence is invalid") from exc


def _read_verified_json(path: Path, schema: str) -> dict[str, object]:
    try:
        payload = _object(json.loads(path.read_text(encoding="utf-8")))
        declared = payload.get("content_hash")
        body = {key: value for key, value in payload.items() if key != "content_hash"}
        if payload.get("schema_version") != schema or not isinstance(declared, str):
            raise ValueError("schema or hash is invalid")
        if ContentHash(body) != declared:
            raise ValueError("content hash is invalid")
        return payload
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise TrainingRepackEvidenceError(f"history repack document is invalid: {path.name}") from exc


def _object(raw: object) -> dict[str, object]:
    if not isinstance(raw, dict) or any(not isinstance(key, str) for key in raw):
        raise TypeError("history repack document must be an object")
    return cast(dict[str, object], raw)


def _list(payload: dict[str, object], key: str) -> list[object]:
    sequence = AsSequence(payload[key])
    if sequence is None:
        raise TypeError(f"history repack {key} must be a list")
    return sequence


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


__all__ = ["ReadTomorrowTrainingMemoryEvidence", "VerifyHistoryRepackTraining", "TrainingRepackEvidenceError"]
