"""Explicit offline conversion of legacy qfq routing to a separate local directory."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from trader.download.domain.published_history import PublishedHistoryWindow
from trader.download.domain.qfq_window import QFQ_WINDOWS
from trader.download.infra.history_control_repository import HistoryMaintenanceLock
from trader.download.infra.qfq_codec import decode_cell
from trader.download.infra.qfq_sqlite import QFQ_SHARD_NAMES, SQLiteQfqWindowCache, qfq_shard_name

_LEGACY_NAME = re.compile(r"(?:[0-9]{5}(?:-[1-9][0-9]{0,3})?|range-[0-9]{5}-[0-9]{4})\.sqlite3")


@dataclass(frozen=True)
class LegacyQfqProfile:
    root: Path
    profile: str
    membership: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class QfqMigrationResult:
    codes: int
    rows: int
    source_files: int
    target_files: int
    source_bytes: int
    target_bytes: int


def inspect_legacy_qfq(source_root: Path) -> tuple[LegacyQfqProfile, ...]:
    profiles: list[LegacyQfqProfile] = []
    if source_root.is_symlink():
        raise ValueError("qfq source must not be a symlink")
    for profile, _ in QFQ_WINDOWS:
        root = source_root / profile
        index = root / "index.json"
        if root.is_symlink() or index.is_symlink():
            raise ValueError("qfq legacy index unsafe")
        value: object = json.loads(index.read_text(encoding="ascii"))
        if not isinstance(value, dict) or not value:
            raise ValueError("qfq legacy index invalid")
        membership: list[tuple[str, str]] = []
        for code, name in value.items():
            if not isinstance(code, str) or not isinstance(name, str) or not _LEGACY_NAME.fullmatch(name):
                raise ValueError("qfq legacy routing invalid")
            qfq_shard_name(code)
            membership.append((code, name))
        for name in sorted({name for _, name in membership}):
            _check_source(root / name)
        profiles.append(LegacyQfqProfile(root, profile, tuple(sorted(membership))))
    return tuple(profiles)


def _check_source(path: Path) -> None:
    if not path.is_file() or path.is_symlink():
        raise ValueError("qfq source shard unavailable")
    for suffix in ("-wal", "-journal"):
        journal = Path(f"{path}{suffix}")
        if journal.is_symlink():
            raise ValueError("qfq source journal unsafe")
        if journal.exists() and journal.stat().st_size:
            with journal.open("rb") as stream:
                header = stream.read(8)
            if suffix == "-wal" or header != bytes(8):
                raise ValueError("qfq source has pending writes")


def legacy_windows(
    profile: LegacyQfqProfile, codes: Sequence[str] | None = None
) -> Iterator[tuple[PublishedHistoryWindow, str]]:
    allowed = frozenset(codes) if codes is not None else None
    grouped: dict[str, list[str]] = {}
    for code, name in profile.membership:
        if allowed is None or code in allowed:
            grouped.setdefault(name, []).append(code)
    for name, requested in grouped.items():
        path = profile.root / name
        _check_source(path)
        with (
            closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=5)) as connection,
            connection,
        ):
            connection.execute("BEGIN")
            for code in requested:
                yield _legacy_window(connection, code, dict(QFQ_WINDOWS)[profile.profile])


def _legacy_window(connection: sqlite3.Connection, code: str, sessions: int) -> tuple[PublishedHistoryWindow, str]:
    rows = tuple(connection.execute("SELECT day,payload FROM bars WHERE code=? ORDER BY day", (code,)))
    identity = connection.execute("SELECT digest,source,cutoff,dates FROM identities WHERE code=?", (code,)).fetchone()
    digest = hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode("ascii")).hexdigest()
    if (
        not rows
        or len(rows) > sessions
        or identity is None
        or digest != identity[0]
        or rows[-1][0] != identity[2]
        or [day for day, _ in rows] != json.loads(identity[3])
    ):
        raise ValueError("qfq legacy identity mismatch")
    return PublishedHistoryWindow(code, tuple(decode_cell(code, day, payload) for day, payload in rows)), str(
        identity[1]
    )


def legacy_fingerprint(profiles: tuple[LegacyQfqProfile, ...]) -> str:
    digest = hashlib.sha256()
    for profile in profiles:
        paths = (
            profile.root / "index.json",
            *(profile.root / name for name in sorted({n for _, n in profile.membership})),
        )
        for path in paths:
            digest.update(f"{profile.profile}/{path.name}".encode("ascii"))
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
    return digest.hexdigest()


def _verify_target(profiles: tuple[LegacyQfqProfile, ...], target_root: Path) -> tuple[int, int]:
    codes = rows = 0
    for profile in profiles:
        cache = SQLiteQfqWindowCache(target_root, profile.profile)
        if cache.codes() != frozenset(code for code, _ in profile.membership):
            raise ValueError("qfq migrated universe mismatch")
        for window, source in legacy_windows(profile):
            if cache.read_code(window.code) != window or cache.source_identity(window.code) != source:
                raise ValueError("qfq migrated window mismatch")
            codes += 1
            rows += len(window.cells)
        for path in cache.root.glob("*.sqlite3"):
            with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as connection:
                if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                    raise ValueError("qfq migrated integrity failure")
    return codes, rows


def build_qfq_layout(source_root: Path, target_root: Path, lock_path: Path) -> QfqMigrationResult:
    """Build and verify a new directory; never replace or delete the source."""
    source = source_root.resolve()
    target = target_root.resolve()
    if source == target or source in target.parents or target in source.parents:
        raise ValueError("qfq migration paths overlap")
    staging = target.with_name(target.name + ".building")
    if target_root.is_symlink() or target.exists() or staging.exists():
        raise ValueError("qfq migration target must be absent")
    with HistoryMaintenanceLock(lock_path):
        profiles = inspect_legacy_qfq(source_root)
        fingerprint = legacy_fingerprint(profiles)
        staging.mkdir(parents=True)
        for profile in profiles:
            cache = SQLiteQfqWindowCache(staging, profile.profile)
            for window, identity in legacy_windows(profile):
                cache.replace_window(window, identity)
            # One-time offline packing; routine refresh never compacts or copies a database.
            for path in cache.root.glob("*.sqlite3"):
                with closing(sqlite3.connect(path, timeout=5)) as connection:
                    connection.execute("PRAGMA synchronous=FULL")
                    connection.execute("VACUUM")
        codes, rows = _verify_target(profiles, staging)
        if legacy_fingerprint(profiles) != fingerprint:
            raise ValueError("qfq migration source changed")
        files = tuple(staging / profile.profile / name for profile in profiles for name in QFQ_SHARD_NAMES)
        existing = tuple(path for path in files if path.is_file())
        result = QfqMigrationResult(
            codes,
            rows,
            sum(len({name for _, name in profile.membership}) for profile in profiles),
            len(existing),
            sum(
                (profile.root / name).stat().st_size
                for profile in profiles
                for name in {n for _, n in profile.membership}
            ),
            sum(path.stat().st_size for path in existing),
        )
        # FULL SQLite commits make files durable; sync directories before publishing the target.
        for directory in (*(staging / profile.profile for profile in profiles), staging):
            _sync_directory(directory)
        if target.exists() or target.is_symlink():
            raise ValueError("qfq migration target appeared during build")
        os.rename(staging, target)
        _sync_directory(target.parent)
        return result


def _sync_directory(path: Path) -> None:
    if os.name != "nt":
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
