#!/usr/bin/env python3
"""Build and verify a data candidate from an external, stopped legacy workspace copy."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.runtime_migration import (  # noqa: E402
    FileDigest,
    ValidateDatabase,
)
from trader.infra.process_lock import ProcessLock  # noqa: E402

MANIFEST_NAME = "migration-manifest.json"


class MigrationError(RuntimeError):
    """The isolated candidate cannot preserve a source identity."""


@dataclass(frozen=True)
class MigrationManifest:
    source_hash: str
    candidate_hash: str
    file_count: int
    total_bytes: int
    regenerate: tuple[str, ...]
    cleanup_candidates: tuple[str, ...]


def ProjectManifest(value: MigrationManifest) -> dict[str, object]:
    return {
        "schema_version": "trader_data_layout_migration",
        "source_hash": value.source_hash,
        "candidate_hash": value.candidate_hash,
        "file_count": value.file_count,
        "total_bytes": value.total_bytes,
        "regenerate": value.regenerate,
        "cleanup_candidates": value.cleanup_candidates,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True)
    build = subparsers.add_parser("build", help="Prepare a candidate; never replace the source or an existing target.")
    build.add_argument(
        "--source", type=Path, required=True, help="external workspace copy containing data and .runtime/trader"
    )
    build.add_argument("--target", type=Path, required=True, help="new external data directory")
    build.add_argument(
        "--default-profile", choices=("v2", "v3"), required=True, help="documented identity of the old unscoped runtime"
    )
    verify = subparsers.add_parser("verify", help="Read-only verification of an isolated candidate.")
    verify.add_argument("--source", type=Path, required=True)
    rollback_parser = subparsers.add_parser("rollback", help="Explicitly restore an external candidate backup.")
    rollback_parser.add_argument("--target", type=Path, required=True)
    rollback_parser.add_argument("--backup", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.action == "build":
            manifest = migrate(args.source, args.target, default_profile=args.default_profile)
            result = {"state": "candidate_verified", **ProjectManifest(manifest)}
        elif args.action == "verify":
            result = {"state": "verified", **ProjectManifest(inspect_layout(args.source))}
        else:
            rollback(args.target, args.backup)
            result = {"state": "rolled_back"}
    except (MigrationError, OSError, sqlite3.Error, ValueError, KeyError, TypeError) as exc:
        result = {"state": "failed", "error_code": _error_code(exc)}
        print(json.dumps(result, sort_keys=True))
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


def migrate(source: Path, target: Path, *, default_profile: str) -> MigrationManifest:
    source = _external_path(source)
    target = _external_path(target)
    if default_profile not in ("v2", "v3"):
        raise MigrationError("migration_default_profile_required")
    if source == target or source in target.parents or target in source.parents:
        raise MigrationError("migration_source_target_overlap")
    if not source.is_dir() or not ((source / "data").is_dir() or (source / ".runtime/trader").is_dir()):
        raise MigrationError("migration_requires_external_workspace_copy")
    staging = target.with_name("." + target.name + ".staging")
    with ProcessLock(target.with_name("." + target.name + ".migration.lock")):
        if target.exists() or staging.exists():
            raise MigrationError("migration_target_or_staging_exists")
        before, _count, _size = _fingerprint(source, validate=True)
        staging.mkdir(parents=True)
        runtime = source / ".runtime/trader"
        # Company facts require a fresh extraction; legacy evidence is never copied into the new contract.
        # DeepSeek has no database import. A new state file quarantines its
        # initialization day, preserving the next-Shanghai-day safety boundary.
        # Freeze records, outcomes, traces and research schemas start empty.
        # Old evidence stays in the source release; no import can grant new formal authority.
        after, _count, _size = _fingerprint(source, validate=True)
        if after != before:
            raise MigrationError("migration_source_changed")
        digest, count, size = _fingerprint(staging, validate=True)
        cleanup = tuple(
            name
            for name in ("evidence_cache", "runtime.sqlite3", "preregistered", "historical_research_baseline")
            if (runtime / name).exists()
        )
        manifest = MigrationManifest(
            before,
            digest,
            count,
            size,
            (
                "market_reference",
                "industry_reference",
                "market_history",
                "daily_window",
                "models",
                "training_samples",
                "company_facts",
                "freeze_records",
                "publication_sequence",
                "outcomes",
                "freeze_traces",
                "research_experiments",
            ),
            cleanup,
        )
        (staging / MANIFEST_NAME).write_text(json.dumps(ProjectManifest(manifest), sort_keys=True, indent=2) + "\n")
        inspect_layout(staging)
        _flush_candidate(staging)
        os.replace(staging, target)
        _fsync_directory(target.parent)
        return manifest


def _flush_candidate(root: Path) -> None:
    for path in (*_files(root), root / MANIFEST_NAME):
        with path.open("rb") as stream:
            os.fsync(stream.fileno())
    for directory in sorted(
        (path for path in root.rglob("*") if path.is_dir()), key=lambda path: len(path.parts), reverse=True
    ):
        _fsync_directory(directory)
    _fsync_directory(root)


def inspect_layout(source: Path) -> MigrationManifest:
    root = _external_path(source)
    raw = json.loads((root / MANIFEST_NAME).read_text())
    if raw.get("schema_version") != "trader_data_layout_migration":
        raise MigrationError("migration_manifest_invalid")
    digest, count, size = _fingerprint(root, validate=True)
    if (raw["candidate_hash"], raw["file_count"], raw["total_bytes"]) != (digest, count, size):
        raise MigrationError("migration_candidate_digest_changed")
    return MigrationManifest(
        raw["source_hash"], digest, count, size, tuple(raw["regenerate"]), tuple(raw["cleanup_candidates"])
    )


def _files(root: Path) -> tuple[Path, ...]:
    files: list[Path] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise MigrationError("migration_symlink_forbidden")
        if (
            path.is_file()
            and path.name != MANIFEST_NAME
            and not path.name.endswith((".lock", "-shm", "-wal", "-journal"))
        ):
            files.append(path)
    return tuple(files)


def _fingerprint(root: Path, *, validate: bool) -> tuple[str, int, int]:
    digest = hashlib.sha256()
    files = _files(root)
    size = 0
    for path in files:
        if validate and path.suffix == ".sqlite3":
            ValidateDatabase(path)
        length = path.stat().st_size
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(length.to_bytes(8, "big"))
        digest.update(FileDigest(path).encode("ascii"))
        size += length
    return digest.hexdigest(), len(files), size


def rollback(target: Path, backup: Path) -> None:
    target, backup = _external_path(target), _external_path(backup)
    if target == backup or target in backup.parents or backup in target.parents or not backup.is_dir():
        raise MigrationError("migration_rollback_paths_invalid")
    failed = target.with_name("." + target.name + ".failed")
    with ProcessLock(target.with_name("." + target.name + ".migration.lock")):
        if failed.exists():
            raise MigrationError("migration_rollback_candidate_exists")
        if target.exists():
            os.replace(target, failed)
        os.replace(backup, target)
        _fsync_directory(target.parent)


def _external_path(path: Path) -> Path:
    if not path.expanduser().is_absolute() or path.is_symlink():
        raise MigrationError("migration_requires_absolute_external_paths")
    resolved = path.expanduser().resolve()
    if resolved == PROJECT_ROOT or PROJECT_ROOT in resolved.parents:
        raise MigrationError("migration_paths_must_be_outside_repository")
    return resolved


def _fsync_directory(path: Path) -> None:
    if os.name != "nt":
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _error_code(exc: BaseException) -> str:
    value = str(exc)
    return (
        value
        if value.startswith("migration_") and value.isidentifier() and len(value) < 100
        else "data_migration_failed"
    )


if __name__ == "__main__":
    raise SystemExit(main())
