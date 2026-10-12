#!/usr/bin/env python3
"""Prepare or roll back an isolated Trader data-layout copy.

The command refuses paths inside the repository. It validates SQLite files in
read-only mode, writes a manifest into a sibling staging directory, and swaps
the completed copy with one ``os.replace``. No active runtime or training data
can be changed accidentally.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from trader.recommendation.infra.persistence.issuer_eligibility import SQLiteIssuerEligibilityIndex

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_NAME = "migration-manifest.json"


class MigrationError(RuntimeError):
    """Raised when an isolated migration cannot be completed safely."""


@dataclass(frozen=True)
class MigrationManifest:
    schema_version: str
    source: str
    source_hash: str
    file_count: int
    total_bytes: int

    def as_json(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "source": self.source,
            "source_hash": self.source_hash,
            "file_count": self.file_count,
            "total_bytes": self.total_bytes,
        }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True)
    build = subparsers.add_parser("build", help="Validate and atomically publish an isolated copy.")
    build.add_argument("--source", type=Path, required=True)
    build.add_argument("--target", type=Path, required=True)
    references = subparsers.add_parser("build-references", help="Prepare generated references in an isolated copy.")
    references.add_argument("--source", type=Path, required=True)
    references.add_argument("--eligibility-database", type=Path, required=True)
    references.add_argument("--target", type=Path, required=True)
    rollback = subparsers.add_parser("rollback", help="Restore a previously retained target backup.")
    rollback.add_argument("--target", type=Path, required=True)
    rollback.add_argument("--backup", type=Path, required=True)
    verify = subparsers.add_parser("verify", help="Validate a source layout without writing anything.")
    verify.add_argument("--source", type=Path, required=True)
    verify_references = subparsers.add_parser("verify-references", help="Validate an isolated reference source.")
    verify_references.add_argument("--source", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.action == "build":
            manifest = migrate(args.source, args.target)
            result = {"state": "published", **manifest.as_json()}
        elif args.action == "build-references":
            manifest = migrate(
                args.source,
                args.target,
                reference_layout=True,
                eligibility_database=args.eligibility_database,
            )
            result = {"state": "published", **manifest.as_json()}
        elif args.action == "verify":
            manifest = inspect_layout(args.source)
            result = {"state": "verified", **manifest.as_json()}
        elif args.action == "verify-references":
            manifest = inspect_layout(args.source, reference_layout=True)
            result = {"state": "verified", **manifest.as_json()}
        else:
            rollback(args.target, args.backup)
            result = {"state": "rolled_back", "target": str(args.target.resolve())}
    except (MigrationError, OSError, sqlite3.Error, ValueError) as exc:
        print(json.dumps({"state": "failed", "error_code": _error_code(exc)}, sort_keys=True))
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


def migrate(
    source: Path,
    target: Path,
    *,
    reference_layout: bool = False,
    eligibility_database: Path | None = None,
) -> MigrationManifest:
    source_path = _external_directory(source)
    target_path = _external_path(target)
    inspect_layout(source_path, reference_layout=reference_layout)
    eligibility_path = _external_file(eligibility_database) if eligibility_database is not None else None
    if reference_layout != (eligibility_path is not None):
        raise MigrationError("reference migration requires one legacy eligibility database")
    lock_path = target_path.parent / f".{target_path.name}.migration.lock"
    staging_path = target_path.parent / f".{target_path.name}.staging"
    backup_path = target_path.parent / f".{target_path.name}.previous"
    _acquire_lock(lock_path)
    try:
        if staging_path.exists():
            shutil.rmtree(staging_path)
        staging_path.mkdir(parents=True)
        for path in _files(source_path, reference_layout=reference_layout):
            destination = staging_path / path.relative_to(source_path)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)
        if eligibility_path is not None:
            SQLiteIssuerEligibilityIndex.migrate_legacy_database(eligibility_path, staging_path)
        manifest = _manifest(
            staging_path,
            schema_version="trader_reference_layout_migration" if reference_layout else "trader_data_layout_migration",
            source=f"{source_path};{eligibility_path}" if eligibility_path is not None else str(source_path),
            reference_layout=reference_layout,
        )
        (staging_path / MANIFEST_NAME).write_text(
            json.dumps(manifest.as_json(), ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        if target_path.exists():
            if backup_path.exists():
                shutil.rmtree(backup_path)
            os.replace(target_path, backup_path)
        os.replace(staging_path, target_path)
        return manifest
    except BaseException:
        if staging_path.exists():
            shutil.rmtree(staging_path)
        if not target_path.exists() and backup_path.exists():
            os.replace(backup_path, target_path)
        raise
    finally:
        _release_lock(lock_path)


def rollback(target: Path, backup: Path) -> None:
    target_path = _external_path(target)
    backup_path = _external_path(backup)
    if not backup_path.is_dir():
        raise MigrationError("rollback backup does not exist")
    lock_path = target_path.parent / f".{target_path.name}.migration.lock"
    _acquire_lock(lock_path)
    try:
        failed_path = target_path.parent / f".{target_path.name}.failed"
        if failed_path.exists():
            shutil.rmtree(failed_path)
        if target_path.exists():
            os.replace(target_path, failed_path)
        os.replace(backup_path, target_path)
        if failed_path.exists():
            shutil.rmtree(failed_path)
    finally:
        _release_lock(lock_path)


def inspect_layout(source: Path, *, reference_layout: bool = False) -> MigrationManifest:
    source_path = _external_directory(source)
    files = tuple(_files(source_path, reference_layout=reference_layout))
    if not files:
        raise MigrationError("source layout is empty")
    if reference_layout:
        _check_reference_layout(source_path, files)
    for path in files:
        if path.suffix == ".sqlite3":
            _check_sqlite(path)
    return _manifest(
        source_path,
        schema_version="trader_reference_layout_migration" if reference_layout else "trader_data_layout_migration",
        source=str(source_path),
        reference_layout=reference_layout,
    )


def _manifest(root: Path, *, schema_version: str, source: str, reference_layout: bool) -> MigrationManifest:
    digest = hashlib.sha256()
    files = tuple(_files(root, reference_layout=reference_layout))
    total_bytes = 0
    for path in files:
        relative = path.relative_to(root).as_posix().encode("utf-8")
        payload = path.read_bytes()
        digest.update(relative)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
        total_bytes += len(payload)
    return MigrationManifest(schema_version, source, digest.hexdigest(), len(files), total_bytes)


def _files(root: Path, *, reference_layout: bool = False) -> Iterable[Path]:
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise MigrationError("migration source cannot contain symbolic links")
        if not path.is_file() or path.name in {".maintenance.lock", ".migration.lock"}:
            continue
        if reference_layout and path.name in {
            ".historical_st.lock",
            "industry_mapping.json",
            ".industry_mapping.rollback.json",
        }:
            continue
        if path.is_file():
            yield path


def _check_reference_layout(root: Path, files: tuple[Path, ...]) -> None:
    names = {path.relative_to(root).as_posix() for path in files}
    if "historical_st.json" not in names or "industry_queries.sqlite3" not in names:
        raise MigrationError("reference source is missing ST evidence or industry queries")
    evidence = json.loads((root / "historical_st.json").read_text(encoding="utf-8"))
    if not isinstance(evidence, dict):
        raise MigrationError("historical ST evidence is invalid")
    for code, raw in evidence.items():
        if (
            not isinstance(code, str)
            or len(code) != 6
            or not code.isdigit()
            or not isinstance(raw, list)
            or len(raw) != 2
            or not isinstance(raw[0], str)
            or raw[1] not in {"ever_st", "clear", "unknown"}
        ):
            raise MigrationError("historical ST evidence row is invalid")
        date.fromisoformat(raw[0])


def _check_sqlite(path: Path) -> None:
    uri = f"file:{path.as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        result = connection.execute("PRAGMA integrity_check").fetchone()
    if result != ("ok",):
        raise MigrationError(f"sqlite integrity check failed: {path.name}")


def _external_directory(path: Path) -> Path:
    resolved = _external_path(path)
    if not resolved.is_dir():
        raise MigrationError(f"source directory does not exist: {resolved}")
    return resolved


def _external_file(path: Path | None) -> Path:
    if path is None:
        raise MigrationError("external file path is required")
    resolved = _external_path(path)
    if not resolved.is_file():
        raise MigrationError(f"source file does not exist: {resolved}")
    return resolved


def _external_path(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    try:
        resolved.relative_to(PROJECT_ROOT)
    except ValueError:
        return resolved
    raise MigrationError("migration paths must be outside the repository")


def _acquire_lock(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise MigrationError("another migration is already running") from exc
    os.close(descriptor)


def _release_lock(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _error_code(exc: BaseException) -> str:
    text = str(exc).strip().lower().replace(" ", "_")
    return text if text and len(text) <= 96 and text.isidentifier() else "data_migration_failed"


if __name__ == "__main__":
    raise SystemExit(main())
