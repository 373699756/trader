"""Stable small SQLite shards with exact no-op writes and local resume state."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import closing
from datetime import date
from pathlib import Path

from trader.download.domain.published_history import PublishedHistoryManifest, PublishedHistoryWindow
from trader.download.domain.qfq_window import QFQ_FILE_LIMIT_BYTES, QFQ_SPLIT_BYTES, QFQ_WINDOWS
from trader.download.infra.qfq_codec import decode_cell, encode_cell
from trader.infra.atomic_files.json import atomic_write_json

_PAGE_BYTES = 4096
_SCHEMA = """
CREATE TABLE bars(code TEXT NOT NULL, day TEXT NOT NULL, payload TEXT NOT NULL,
 PRIMARY KEY(code,day)) WITHOUT ROWID;
CREATE TABLE identities(code TEXT PRIMARY KEY, digest TEXT NOT NULL, source TEXT NOT NULL,
 cutoff TEXT NOT NULL, dates TEXT NOT NULL) WITHOUT ROWID;
"""


class SQLiteQfqWindowCache:
    """Download-owned derived cache; history remains the full research authority."""

    def __init__(self, root: Path, profile: str) -> None:
        windows = dict(QFQ_WINDOWS)
        if profile not in windows:
            raise ValueError("qfq profile must be v2 or v3")
        self.root = root / profile
        self.sessions = windows[profile]
        self._index_stamp: tuple[int, int, int] | None = None
        self._index: dict[str, str] = {}

    def _membership(self) -> dict[str, str]:
        path = self.root / "index.json"
        if not path.exists():
            return {}
        stat = path.stat()
        stamp = (stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size)
        if stamp == self._index_stamp:
            return dict(self._index)
        value: object = json.loads(path.read_text(encoding="ascii"))
        if not isinstance(value, dict) or any(
            not isinstance(code, str)
            or len(code) != 6
            or not code.isdigit()
            or not isinstance(name, str)
            or not _valid_name(name)
            for code, name in value.items()
        ):
            raise ValueError("qfq index invalid")
        self._index = value
        self._index_stamp = stamp
        return dict(value)

    def _publish_membership(self, membership: dict[str, str]) -> bool:
        path = self.root / "index.json"
        payload = json.dumps(membership, sort_keys=True, separators=(",", ":"))
        if path.exists() and path.read_text(encoding="ascii") == payload:
            return False
        atomic_write_json(path, membership)
        return True

    def codes(self) -> frozenset[str]:
        return frozenset(self._membership())

    def read_code(self, code: str) -> PublishedHistoryWindow:
        name = self._membership().get(code)
        if name is None:
            return PublishedHistoryWindow(code, ())
        try:
            with closing(self._read_connection(name)) as connection, connection:
                rows = self._read_rows(connection, code)
        except sqlite3.Error as exc:
            raise RuntimeError("qfq shard read failed") from exc
        return PublishedHistoryWindow(code, tuple(decode_cell(code, day, payload) for day, payload in rows))

    def _read_rows(self, connection: sqlite3.Connection, code: str) -> tuple[tuple[str, str], ...]:
        if not connection.in_transaction:
            connection.execute("BEGIN")
        rows = tuple(connection.execute("SELECT day,payload FROM bars WHERE code=? ORDER BY day", (code,)))
        identity = connection.execute("SELECT digest FROM identities WHERE code=?", (code,)).fetchone()
        digest = hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode("ascii")).hexdigest()
        if not rows or len(rows) > self.sessions or identity is None or digest != identity[0]:
            raise RuntimeError("qfq code identity mismatch")
        return rows

    def replace_window(self, window: PublishedHistoryWindow, source_identity: str) -> tuple[tuple[str, ...], int]:
        try:
            return self._replace_window(window, source_identity)
        except sqlite3.Error as exc:
            raise RuntimeError("qfq shard update failed") from exc

    def source_identity(self, code: str) -> str | None:
        name = self._membership().get(code)
        if name is None:
            return None
        try:
            with closing(self._read_connection(name)) as connection:
                row = connection.execute("SELECT source FROM identities WHERE code=?", (code,)).fetchone()
        except sqlite3.Error as exc:
            raise RuntimeError("qfq source identity unavailable") from exc
        return str(row[0]) if row is not None else None

    def _replace_window(self, window: PublishedHistoryWindow, source_identity: str) -> tuple[tuple[str, ...], int]:
        cells = window.cells[-self.sessions :]
        if not cells:
            return (), 0
        target = tuple((cell.trade_date.isoformat(), encode_cell(cell)) for cell in cells)
        membership = self._membership()
        old_name = membership.get(window.code)
        if old_name is not None:
            with closing(self._read_connection(old_name)) as connection:
                old = tuple(
                    connection.execute("SELECT day,payload FROM bars WHERE code=? ORDER BY day", (window.code,))
                )
                identity = connection.execute("SELECT source FROM identities WHERE code=?", (window.code,)).fetchone()
            if old == target and identity is not None and identity[0] == source_identity:
                return (), 0
        else:
            old = ()
        self.root.mkdir(parents=True, exist_ok=True)
        name = old_name or self._choose_shard(window.code, target, membership)
        try:
            self._write_code(name, window.code, target, source_identity)
        except sqlite3.OperationalError as exc:
            if "full" not in str(exc).lower():
                raise
            name = self._choose_shard(window.code, target, membership, exclude=name)
            self._write_code(name, window.code, target, source_identity)
        membership[window.code] = name
        # Publish routing only after durable data; an interrupted unreferenced
        # row is safe and will be reused on the next attempt.
        index_changed = self._publish_membership(membership)
        changed = [name]
        if index_changed:
            changed.append("index.json")
        if old_name is not None and name != old_name:
            with closing(self._write_connection(old_name)) as connection, connection:
                connection.execute("DELETE FROM bars WHERE code=?", (window.code,))
                connection.execute("DELETE FROM identities WHERE code=?", (window.code,))
            changed.append(old_name)
        old_by_day = dict(old)
        new_by_day = dict(target)
        count = sum(old_by_day.get(day) != new_by_day.get(day) for day in old_by_day.keys() | new_by_day.keys())
        return tuple(str(self.root.name + "/" + item) for item in changed), count

    def _choose_shard(
        self, code: str, rows: tuple[tuple[str, str], ...], membership: dict[str, str], *, exclude: str | None = None
    ) -> str:
        prefix = f"{int(code) // 64:05d}"
        # SQLite pages/index overhead has ample reserve; hard page cap is the
        # final guard. Names and existing code routing never get rebalanced.
        reserve = sum(len(payload.encode("ascii")) + 160 for _, payload in rows) * 2 + 65536
        for part in range(10000):
            name = f"{prefix}.sqlite3" if part == 0 else f"{prefix}-{part}.sqlite3"
            if name == exclude:
                continue
            path = self.root / name
            if not path.exists():
                return name
            if (
                path.stat().st_size + reserve < QFQ_SPLIT_BYTES
                and sum(item == name for item in membership.values()) < 64
            ):
                return name
        raise RuntimeError("qfq shard capacity exhausted")

    def _write_code(self, name: str, code: str, rows: tuple[tuple[str, str], ...], source: str) -> None:
        with closing(self._write_connection(name)) as connection, connection:
            existing = dict(connection.execute("SELECT day,payload FROM bars WHERE code=?", (code,)))
            incoming = dict(rows)
            connection.executemany(
                "DELETE FROM bars WHERE code=? AND day=?", ((code, day) for day in existing.keys() - incoming.keys())
            )
            connection.executemany(
                "INSERT OR REPLACE INTO bars VALUES (?,?,?)",
                ((code, day, payload) for day, payload in rows if existing.get(day) != payload),
            )
            digest = hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode("ascii")).hexdigest()
            connection.execute(
                "INSERT OR REPLACE INTO identities VALUES (?,?,?,?,?)",
                (code, digest, source, rows[-1][0], json.dumps([day for day, _ in rows], separators=(",", ":"))),
            )

    def _write_connection(self, name: str) -> sqlite3.Connection:
        path = self.root / name
        connection = sqlite3.connect(path, timeout=5)
        try:
            connection.execute("PRAGMA journal_mode=DELETE")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute(f"PRAGMA max_page_count={(QFQ_SPLIT_BYTES - 1) // _PAGE_BYTES}")
            if not connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchone():
                connection.executescript(_SCHEMA)
            return connection
        except sqlite3.Error:
            connection.close()
            raise

    def _read_connection(self, name: str) -> sqlite3.Connection:
        path = self.root / name
        if not path.is_file() or path.stat().st_size >= QFQ_FILE_LIMIT_BYTES:
            raise RuntimeError("qfq shard missing or oversized")
        try:
            return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=5)
        except sqlite3.Error as exc:
            raise RuntimeError("qfq shard unavailable") from exc

    def manifest(self) -> PublishedHistoryManifest | None:
        try:
            return self._manifest()
        except (sqlite3.Error, OSError, ValueError) as exc:
            raise RuntimeError("qfq manifest unavailable") from exc

    def _manifest(self) -> PublishedHistoryManifest | None:
        membership = self._membership()
        if not membership:
            return None
        identities: list[tuple[str, str]] = []
        dates: set[date] = set()
        for name in sorted(set(membership.values())):
            with closing(self._read_connection(name)) as connection:
                for code, digest, calendar_json in connection.execute("SELECT code,digest,dates FROM identities"):
                    if membership.get(code) == name:
                        identities.append((code, digest))
                        dates.update(date.fromisoformat(day) for day in json.loads(calendar_json))
        if not dates or len(identities) != len(membership):
            raise RuntimeError("qfq shard identity incomplete")
        calendar = tuple(sorted(dates))[-self.sessions :]
        digest = hashlib.sha256(json.dumps(sorted(identities)).encode("ascii")).hexdigest()
        return PublishedHistoryManifest(digest, 1, calendar[-1], calendar, tuple(sorted(membership)))

    def iter_windows(self, manifest: PublishedHistoryManifest, *, sessions: int) -> Iterator[PublishedHistoryWindow]:
        membership = self._membership()
        for name in sorted(set(membership.values())):
            codes = tuple(code for code in manifest.universe_codes if membership.get(code) == name)
            yield from self.read_windows(manifest, codes, sessions=sessions)

    def read_windows(
        self, manifest: PublishedHistoryManifest, codes: Sequence[str], *, sessions: int
    ) -> tuple[PublishedHistoryWindow, ...]:
        try:
            return self._read_windows(manifest, codes, sessions=sessions)
        except sqlite3.Error as exc:
            raise RuntimeError("qfq windows unavailable") from exc

    def _read_windows(
        self, manifest: PublishedHistoryManifest, codes: Sequence[str], *, sessions: int
    ) -> tuple[PublishedHistoryWindow, ...]:
        if not 1 <= sessions <= self.sessions:
            raise ValueError("qfq read window invalid")
        membership = self._membership()
        grouped: dict[str, list[str]] = {}
        for code in dict.fromkeys(codes):
            if code in membership:
                grouped.setdefault(membership[code], []).append(code)
        result: list[PublishedHistoryWindow] = []
        for name, requested in grouped.items():
            with closing(self._read_connection(name)) as connection, connection:
                for code in requested:
                    rows = self._read_rows(connection, code)[-sessions:]
                    result.append(
                        PublishedHistoryWindow(code, tuple(decode_cell(code, day, payload) for day, payload in rows))
                    )
        return tuple(sorted(result, key=lambda item: item.code))


def _valid_name(name: str) -> bool:
    parts = name.removesuffix(".sqlite3").split("-")
    return (
        len(parts) in (1, 2)
        and len(parts[0]) == 5
        and parts[0].isascii()
        and parts[0].isdigit()
        and (
            len(parts) == 1
            or (
                parts[1].isascii()
                and parts[1].isdigit()
                and 1 <= int(parts[1]) < 10000
                and str(int(parts[1])) == parts[1]
            )
        )
        and name.endswith(".sqlite3")
    )
