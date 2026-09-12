"""Crash-safe publication of the four fixed Tomorrow V3 training files."""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import cast

from trader.infra.scoring.artifact_hashing import artifact_content_hash

_ACTIVE_BUNDLE_NAME = "active-bundle.json"
_PUBLICATION_JOURNAL_NAME = ".bundle-publication.json"
_PRODUCTION_NAMES = ("training-input.json", "report.json", "model.json", _ACTIVE_BUNDLE_NAME)
_ROLLBACK_DIRECTORY = re.compile(r"^\.bundle-rollback\.[A-Za-z0-9_-]+$")


@dataclass(frozen=True)
class ActiveTomorrowBundle:
    """Validated identity of the four files selected by the commit marker."""

    model_path: Path
    training_input_hash: str
    source_identity_hash: str
    label_cutoff: date
    training_contract_hash: str


@dataclass(frozen=True)
class _PublicationJournal:
    rollback_directory: str
    previous_files: tuple[str, ...]
    new_pointer_hash: str


def publish_tomorrow_bundle(
    staging: Path,
    output_root: Path,
    *,
    training_input_hash: str,
    source_identity_hash: str,
    label_cutoff: date,
) -> Path:
    """Validate a staged group and replace the fixed files with recovery evidence."""

    output_root.mkdir(parents=True, exist_ok=True)
    recover_tomorrow_bundle_publication(output_root)
    pointer = _validate_staging(
        staging,
        training_input_hash=training_input_hash,
        source_identity_hash=source_identity_hash,
        label_cutoff=label_cutoff,
    )
    _write_json(staging / _ACTIVE_BUNDLE_NAME, pointer)
    _fsync_directory(staging)

    rollback = Path(tempfile.mkdtemp(prefix=".bundle-rollback.", dir=output_root))
    try:
        previous_files = _backup_previous_files(output_root, rollback)
        journal = _PublicationJournal(
            rollback.name,
            previous_files,
            cast(str, pointer["content_hash"]),
        )
        _write_json(output_root / _PUBLICATION_JOURNAL_NAME, _journal_payload(journal))
        for name in _PRODUCTION_NAMES:
            os.replace(staging / name, output_root / name)
            _fsync_directory(output_root)
        _inspect_active_tomorrow_bundle(output_root, allow_publication=True)
        _finish_publication(output_root, rollback)
    except Exception:
        if (output_root / _PUBLICATION_JOURNAL_NAME).exists():
            recover_tomorrow_bundle_publication(output_root)
        elif rollback.exists():
            shutil.rmtree(rollback)
            _fsync_directory(output_root)
        raise
    finally:
        if staging.exists() and staging.is_dir() and not staging.is_symlink():
            shutil.rmtree(staging)
    return output_root / "model.json"


def locate_active_tomorrow_bundle(output_root: Path) -> Path:
    return inspect_active_tomorrow_bundle(output_root).model_path


def inspect_active_tomorrow_bundle(output_root: Path) -> ActiveTomorrowBundle:
    return _inspect_active_tomorrow_bundle(output_root, allow_publication=False)


def recover_tomorrow_bundle_publication(output_root: Path) -> None:
    """Finish a committed replacement or restore the complete previous file set."""

    journal_path = output_root / _PUBLICATION_JOURNAL_NAME
    if not journal_path.exists():
        return
    journal = _decode_journal(_read_json(journal_path))
    rollback = _rollback_path(output_root, journal.rollback_directory)
    pointer_hash = _pointer_content_hash(output_root / _ACTIVE_BUNDLE_NAME)
    if pointer_hash == journal.new_pointer_hash:
        try:
            _inspect_active_tomorrow_bundle(output_root, allow_publication=True)
        except (OSError, RuntimeError, TypeError, ValueError):
            pass
        else:
            _finish_publication(output_root, rollback)
            return
    _restore_previous_files(output_root, rollback, journal.previous_files)
    _finish_publication(output_root, rollback)


def make_bundle_staging_directory(output_root: Path) -> Path:
    output_root.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=".bundle-staging.", dir=output_root))


def _validate_staging(
    staging: Path,
    *,
    training_input_hash: str,
    source_identity_hash: str,
    label_cutoff: date,
) -> dict[str, object]:
    from trader.infra.scoring.profiles.v3.bundle_codec import load_tomorrow_bundle

    artifact = load_tomorrow_bundle(staging / "model.json")
    if (
        artifact.training_input_hash != training_input_hash
        or artifact.label_cutoff != label_cutoff
        or artifact.source_identity_hash != source_identity_hash
    ):
        raise ValueError("Tomorrow V3 staged bundle active archive identity is inconsistent")
    training_input = _read_json(staging / "training-input.json")
    report = _read_json(staging / "report.json")
    expected_cutoff = label_cutoff.isoformat()
    if training_input.get("label_cutoff") != expected_cutoff or report.get("label_cutoff") != expected_cutoff:
        raise ValueError("Tomorrow V3 staged bundle label cutoff is inconsistent")
    pointer: dict[str, object] = {
        "training_input_document_hash": training_input["content_hash"],
        "report_hash": report["content_hash"],
        "model_hash": artifact.content_hash,
    }
    pointer["content_hash"] = artifact_content_hash(pointer)
    return pointer


def _inspect_active_tomorrow_bundle(output_root: Path, *, allow_publication: bool) -> ActiveTomorrowBundle:
    if not allow_publication and (output_root / _PUBLICATION_JOURNAL_NAME).exists():
        raise RuntimeError("Tomorrow V3 bundle publication is incomplete")
    pointer = _read_json(output_root / _ACTIVE_BUNDLE_NAME)
    expected_fields = {
        "training_input_document_hash",
        "report_hash",
        "model_hash",
        "content_hash",
    }
    declared_hash = pointer.get("content_hash")
    body = {key: value for key, value in pointer.items() if key != "content_hash"}
    hash_fields = expected_fields - {"content_hash"}
    if (
        set(pointer) != expected_fields
        or not isinstance(declared_hash, str)
        or artifact_content_hash(body) != declared_hash
        or not all(_sha256(pointer.get(name)) for name in hash_fields)
    ):
        raise ValueError("Tomorrow V3 active bundle pointer is invalid")
    model = output_root / "model.json"
    if not model.is_file() or model.is_symlink():
        raise FileNotFoundError(model)
    from trader.infra.scoring.profiles.v3.bundle_codec import load_tomorrow_bundle

    artifact = load_tomorrow_bundle(model)
    training_input = _read_json(output_root / "training-input.json")
    report = _read_json(output_root / "report.json")
    if (
        artifact.content_hash != pointer["model_hash"]
        or training_input.get("content_hash") != pointer["training_input_document_hash"]
        or report.get("content_hash") != pointer["report_hash"]
    ):
        raise ValueError("Tomorrow V3 active bundle pointer does not match its fixed files")
    return ActiveTomorrowBundle(
        model,
        artifact.training_input_hash,
        artifact.source_identity_hash,
        artifact.label_cutoff,
        artifact.training_contract_hash,
    )


def _backup_previous_files(output_root: Path, rollback: Path) -> tuple[str, ...]:
    previous: list[str] = []
    for name in _PRODUCTION_NAMES:
        source = output_root / name
        if not source.exists():
            continue
        if not source.is_file() or source.is_symlink():
            raise ValueError(f"Tomorrow V3 production artifact is not a regular file: {name}")
        _copy_file(source, rollback / name)
        previous.append(name)
    _fsync_directory(rollback)
    return tuple(previous)


def _restore_previous_files(output_root: Path, rollback: Path, previous_files: tuple[str, ...]) -> None:
    if not rollback.is_dir() or rollback.is_symlink():
        raise RuntimeError("Tomorrow V3 publication rollback evidence is unavailable")
    previous = set(previous_files)
    for name in _PRODUCTION_NAMES:
        destination = output_root / name
        if name in previous:
            source = rollback / name
            if not source.is_file() or source.is_symlink():
                raise RuntimeError("Tomorrow V3 publication rollback evidence is incomplete")
            _copy_file(source, destination)
        else:
            destination.unlink(missing_ok=True)
    _fsync_directory(output_root)


def _finish_publication(output_root: Path, rollback: Path) -> None:
    if rollback.exists():
        if not rollback.is_dir() or rollback.is_symlink():
            raise RuntimeError("Tomorrow V3 publication rollback path is invalid")
        shutil.rmtree(rollback)
        _fsync_directory(output_root)
    (output_root / _PUBLICATION_JOURNAL_NAME).unlink(missing_ok=True)
    _fsync_directory(output_root)


def _rollback_path(output_root: Path, name: str) -> Path:
    if _ROLLBACK_DIRECTORY.fullmatch(name) is None:
        raise ValueError("Tomorrow V3 publication rollback directory is invalid")
    rollback = (output_root / name).resolve()
    if rollback.parent != output_root.resolve():
        raise ValueError("Tomorrow V3 publication rollback path escapes the profile directory")
    return rollback


def _decode_journal(payload: dict[str, object]) -> _PublicationJournal:
    expected = {
        "schema_version",
        "rollback_directory",
        "previous_files",
        "new_pointer_hash",
        "content_hash",
    }
    declared_hash = payload.get("content_hash")
    body = {key: value for key, value in payload.items() if key != "content_hash"}
    previous = payload.get("previous_files")
    if (
        set(payload) != expected
        or payload.get("schema_version") != "tomorrow_training_bundle_publication"
        or not isinstance(declared_hash, str)
        or artifact_content_hash(body) != declared_hash
        or not isinstance(payload.get("rollback_directory"), str)
        or not isinstance(previous, list)
        or any(not isinstance(value, str) or value not in _PRODUCTION_NAMES for value in previous)
        or len(previous) != len(set(previous))
        or not _sha256(payload.get("new_pointer_hash"))
    ):
        raise ValueError("Tomorrow V3 publication journal is invalid")
    return _PublicationJournal(
        cast(str, payload["rollback_directory"]),
        tuple(cast(list[str], previous)),
        cast(str, payload["new_pointer_hash"]),
    )


def _journal_payload(journal: _PublicationJournal) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": "tomorrow_training_bundle_publication",
        "rollback_directory": journal.rollback_directory,
        "previous_files": list(journal.previous_files),
        "new_pointer_hash": journal.new_pointer_hash,
    }
    payload["content_hash"] = artifact_content_hash(payload)
    return payload


def _pointer_content_hash(path: Path) -> str | None:
    try:
        payload = _read_json(path)
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None
    value = payload.get("content_hash")
    return value if isinstance(value, str) else None


def _copy_file(source: Path, destination: Path) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        shutil.copyfile(source, temporary)
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        _fsync_directory(destination.parent)
    finally:
        temporary.unlink(missing_ok=True)


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


__all__ = [
    "ActiveTomorrowBundle",
    "inspect_active_tomorrow_bundle",
    "locate_active_tomorrow_bundle",
    "make_bundle_staging_directory",
    "publish_tomorrow_bundle",
    "recover_tomorrow_bundle_publication",
]
