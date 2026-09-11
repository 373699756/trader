"""Crash-safe publication of one hash-bound V3 training artifact group."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import cast

from trader.infra.scoring.artifact_hashing import artifact_content_hash

_POINTER_NAME = "active-bundle.json"


@dataclass(frozen=True)
class ActiveTomorrowBundle:
    """Validated identity of the bundle selected by the active pointer."""

    model_path: Path
    generation: str
    training_input_hash: str
    source_identity_hash: str
    label_cutoff: date


def publish_tomorrow_bundle(
    staging: Path,
    output_root: Path,
    *,
    training_input_hash: str,
    source_identity_hash: str,
    label_cutoff: date,
) -> Path:
    """Validate staging first, then atomically switch a small active pointer."""

    from trader.infra.scoring.profiles.v3.bundle_codec import load_tomorrow_bundle

    artifact = load_tomorrow_bundle(staging / "model.json")
    if (
        artifact.training_input_hash != training_input_hash
        or artifact.label_cutoff != label_cutoff
        or artifact.source_identity_hash != source_identity_hash
    ):
        raise ValueError("Tomorrow V3 staged bundle active archive identity is inconsistent")
    training_input_document = _read_json(staging / "training-input.json")
    report_document = _read_json(staging / "report.json")
    expected_label_cutoff = label_cutoff.isoformat()
    if (
        training_input_document.get("label_cutoff") != expected_label_cutoff
        or report_document.get("label_cutoff") != expected_label_cutoff
    ):
        raise ValueError("Tomorrow V3 staged bundle label cutoff is inconsistent")
    bundle_hash = artifact_content_hash(
        {
            "training_input_document_hash": training_input_document["content_hash"],
            "report_hash": report_document["content_hash"],
            "model_hash": artifact.content_hash,
            "training_input_hash": training_input_hash,
            "label_cutoff": expected_label_cutoff,
            "source_identity_hash": source_identity_hash,
        }
    )
    output_root.mkdir(parents=True, exist_ok=True)
    generations = output_root / "generations"
    generations.mkdir(exist_ok=True)
    destination = generations / bundle_hash
    if destination.exists():
        existing = load_tomorrow_bundle(destination / "model.json")
        if existing.content_hash != artifact.content_hash:
            raise ValueError("Tomorrow V3 bundle generation conflicts")
        shutil.rmtree(staging)
    else:
        _fsync_directory(staging)
        os.replace(staging, destination)
        _fsync_directory(generations)
    pointer: dict[str, object] = {
        "schema_version": "tomorrow_training_active_bundle",
        "generation": bundle_hash,
        "training_input_hash": training_input_hash,
        "label_cutoff": expected_label_cutoff,
        "source_identity_hash": source_identity_hash,
        "training_input_document_hash": training_input_document["content_hash"],
        "report_hash": report_document["content_hash"],
        "model_hash": artifact.content_hash,
    }
    pointer["content_hash"] = artifact_content_hash(pointer)
    _write_json(output_root / _POINTER_NAME, pointer)
    return destination / "model.json"


def locate_active_tomorrow_bundle(output_root: Path) -> Path:
    return inspect_active_tomorrow_bundle(output_root).model_path


def inspect_active_tomorrow_bundle(output_root: Path) -> ActiveTomorrowBundle:
    pointer = _read_json(output_root / _POINTER_NAME)
    stored_hash = pointer.pop("content_hash", None)
    expected_fields = {
        "schema_version",
        "generation",
        "training_input_hash",
        "source_identity_hash",
        "label_cutoff",
        "training_input_document_hash",
        "report_hash",
        "model_hash",
    }
    if (
        not isinstance(stored_hash, str)
        or artifact_content_hash(pointer) != stored_hash
        or set(pointer) != expected_fields
        or pointer.get("schema_version") != "tomorrow_training_active_bundle"
        or not all(_sha256(pointer.get(name)) for name in expected_fields - {"schema_version", "label_cutoff"})
        or not _iso_date(pointer.get("label_cutoff"))
    ):
        raise ValueError("Tomorrow V3 active bundle pointer is invalid")
    generation = cast(str, pointer["generation"])
    model = (output_root / "generations" / generation / "model.json").resolve()
    if output_root.resolve() not in model.parents or not model.is_file():
        raise FileNotFoundError(model)
    from trader.infra.scoring.profiles.v3.bundle_codec import load_tomorrow_bundle

    artifact = load_tomorrow_bundle(model)
    training_input = _read_json(model.with_name("training-input.json"))
    report = _read_json(model.with_name("report.json"))
    if (
        artifact.content_hash != pointer["model_hash"]
        or training_input.get("content_hash") != pointer["training_input_document_hash"]
        or report.get("content_hash") != pointer["report_hash"]
        or artifact.training_input_hash != pointer["training_input_hash"]
        or artifact.label_cutoff.isoformat() != pointer["label_cutoff"]
        or artifact.source_identity_hash != pointer["source_identity_hash"]
    ):
        raise ValueError("Tomorrow V3 active bundle pointer does not match its generation")
    return ActiveTomorrowBundle(
        model,
        generation,
        pointer["training_input_hash"],
        pointer["source_identity_hash"],
        date.fromisoformat(pointer["label_cutoff"]),
    )


def make_bundle_staging_directory(output_root: Path) -> Path:
    output_root.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=".bundle-staging.", dir=output_root))


def _read_json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise TypeError("Tomorrow V3 bundle document must be an object")
    return cast(dict[str, object], value)


def _write_json(path: Path, payload: dict[str, object]) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
        _fsync_directory(path.parent)
    finally:
        Path(temporary_name).unlink(missing_ok=True)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _sha256(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _iso_date(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


__all__ = [
    "ActiveTomorrowBundle",
    "inspect_active_tomorrow_bundle",
    "locate_active_tomorrow_bundle",
    "make_bundle_staging_directory",
    "publish_tomorrow_bundle",
]
