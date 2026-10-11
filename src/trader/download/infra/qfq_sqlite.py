"""Local qfq windows, routed directly by exchange and board without a sidecar index."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import closing
from datetime import date
from pathlib import Path

from trader.download.domain.published_history import PublishedHistoryManifest, PublishedHistoryWindow
from trader.download.domain.qfq_window import QFQ_WINDOWS, QfqWindowSnapshot, QfqWindowState
from trader.download.infra.qfq_codec import decode_cell, encode_cell

QFQ_SHARD_NAMES = (
    "qfq_sse_main.sqlite3",
    "qfq_szse_main.sqlite3",
    "qfq_szse_chinext.sqlite3",
    "qfq_sse_star.sqlite3",
)
_SCHEMA = """
CREATE TABLE bars(code TEXT NOT NULL, day TEXT NOT NULL, payload TEXT NOT NULL,
 PRIMARY KEY(code,day)) WITHOUT ROWID;
CREATE TABLE identities(code TEXT PRIMARY KEY, digest TEXT NOT NULL, source TEXT NOT NULL,
 cutoff TEXT NOT NULL, dates TEXT NOT NULL) WITHOUT ROWID;
"""


def qfq_shard_name(code: str) -> str:
    if len(code) != 6 or not code.isascii() or not code.isdigit():
        raise ValueError("qfq code invalid")
    if code.startswith(("600", "601", "603", "605")):
        return QFQ_SHARD_NAMES[0]
    if code.startswith(("000", "001", "002", "003")):
        return QFQ_SHARD_NAMES[1]
    if code.startswith(("300", "301", "302")):
        return QFQ_SHARD_NAMES[2]
    if code.startswith(("688", "689")):
        return QFQ_SHARD_NAMES[3]
    raise ValueError("qfq code outside supported A-share boards")


def _digest(rows: tuple[tuple[str, str], ...]) -> str:
    return hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode("ascii")).hexdigest()


class SQLiteQfqWindowCache:
    """Single-writer derived cache; complete history remains the research authority."""

    def __init__(self, root: Path, profile: str) -> None:
        windows = dict(QFQ_WINDOWS)
        if profile not in windows:
            raise ValueError("qfq profile must be v2 or v3")
        self.root = root / profile
        self.sessions = windows[profile]

    def _ensure_layout(self) -> None:
        if (self.root / "index.json").exists():
            raise RuntimeError("qfq_legacy_layout_requires_migration")

    def _existing_names(self) -> tuple[str, ...]:
        self._ensure_layout()
        return tuple(name for name in QFQ_SHARD_NAMES if (self.root / name).is_file())

    def recover_pending_transactions(self) -> tuple[str, ...]:
        """Let SQLite roll back hot journals before any read-only window checks."""

        recovered: list[str] = []
        try:
            for name in self._existing_names():
                path = self.root / name
                journal = Path(f"{path}-journal")
                if not journal.is_file() or journal.stat().st_size == 0:
                    continue
                if path.is_symlink() or journal.is_symlink():
                    raise RuntimeError("qfq shard recovery path is unsafe")
                with closing(sqlite3.connect(path, timeout=5)) as connection:
                    if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                        raise RuntimeError("qfq shard recovery integrity check failed")
                recovered.append(f"{self.root.name}/{name}")
        except (OSError, sqlite3.Error) as exc:
            raise RuntimeError("qfq pending transaction recovery failed") from exc
        return tuple(recovered)

    def codes(self) -> frozenset[str]:
        try:
            result: set[str] = set()
            for name in self._existing_names():
                with closing(self._read_connection(name)) as connection:
                    for (code,) in connection.execute("SELECT code FROM identities"):
                        if qfq_shard_name(code) != name:
                            raise RuntimeError("qfq code routed to wrong shard")
                        result.add(code)
            return frozenset(result)
        except sqlite3.Error as exc:
            raise RuntimeError("qfq codes unavailable") from exc

    def read_code(self, code: str) -> PublishedHistoryWindow:
        self._ensure_layout()
        name = qfq_shard_name(code)
        if not (self.root / name).is_file():
            return PublishedHistoryWindow(code, ())
        try:
            with closing(self._read_connection(name)) as connection, connection:
                rows = self._read_rows(connection, code)
        except sqlite3.Error as exc:
            raise RuntimeError("qfq shard read failed") from exc
        return PublishedHistoryWindow(code, tuple(decode_cell(code, day, payload) for day, payload in rows))

    def read_codes(self, codes: Sequence[str]) -> tuple[PublishedHistoryWindow, ...]:
        """Read a bounded batch while sharing one connection per touched board shard."""

        self._ensure_layout()
        grouped: dict[str, list[str]] = {}
        for code in dict.fromkeys(codes):
            grouped.setdefault(qfq_shard_name(code), []).append(code)
        result: list[PublishedHistoryWindow] = []
        try:
            for name, requested in grouped.items():
                if not (self.root / name).is_file():
                    result.extend(PublishedHistoryWindow(code, ()) for code in requested)
                    continue
                with closing(self._read_connection(name)) as connection, connection:
                    for code in requested:
                        rows = self._read_rows(connection, code)
                        result.append(
                            PublishedHistoryWindow(
                                code, tuple(decode_cell(code, day, payload) for day, payload in rows)
                            )
                        )
        except sqlite3.Error as exc:
            raise RuntimeError("qfq batch read failed") from exc
        return tuple(sorted(result, key=lambda window: window.code))

    def inspect_windows(self, allowed_codes: frozenset[str]) -> QfqWindowSnapshot:
        """Verify stored identities in bulk without constructing domain bar objects."""

        if any(len(code) != 6 or not code.isascii() or not code.isdigit() for code in allowed_codes):
            raise ValueError("qfq inspected code set is invalid")
        states: list[QfqWindowState] = []
        try:
            for name in self._existing_names():
                with closing(self._read_connection(name)) as connection, connection:
                    connection.execute("BEGIN")
                    identities = tuple(
                        connection.execute("SELECT code,digest,source,cutoff,dates FROM identities ORDER BY code")
                    )
                    for code, digest, source, cutoff, dates_json in identities:
                        if code not in allowed_codes:
                            continue
                        if qfq_shard_name(code) != name:
                            raise RuntimeError("qfq code routed to wrong shard")
                        rows = tuple(
                            connection.execute("SELECT day,payload FROM bars WHERE code=? ORDER BY day", (code,))
                        )
                        verified = False
                        dates: tuple[date, ...] = ()
                        try:
                            stored_dates: object = json.loads(dates_json)
                            if isinstance(stored_dates, list) and all(isinstance(day, str) for day in stored_dates):
                                dates = tuple(date.fromisoformat(day) for day in stored_dates)
                                row_dates = tuple(date.fromisoformat(day) for day, _payload in rows)
                                verified = (
                                    bool(rows)
                                    and len(rows) <= self.sessions
                                    and dates == row_dates
                                    and cutoff == rows[-1][0]
                                    and digest == _digest(rows)
                                )
                        except (TypeError, ValueError):
                            verified = False
                        states.append(QfqWindowState(str(code), str(source), dates, verified))
        except sqlite3.Error as exc:
            raise RuntimeError("qfq window inspection failed") from exc
        return QfqWindowSnapshot(tuple(sorted(states, key=lambda state: state.code)))

    def _read_rows(self, connection: sqlite3.Connection, code: str) -> tuple[tuple[str, str], ...]:
        if not connection.in_transaction:
            connection.execute("BEGIN")
        rows = tuple(connection.execute("SELECT day,payload FROM bars WHERE code=? ORDER BY day", (code,)))
        identity = connection.execute("SELECT digest FROM identities WHERE code=?", (code,)).fetchone()
        if not rows and identity is None:
            return ()
        if not rows or len(rows) > self.sessions or identity is None or _digest(rows) != identity[0]:
            raise RuntimeError("qfq code identity mismatch")
        return rows

    def source_identity(self, code: str) -> str | None:
        self._ensure_layout()
        name = qfq_shard_name(code)
        if not (self.root / name).is_file():
            return None
        try:
            with closing(self._read_connection(name)) as connection:
                row = connection.execute("SELECT source FROM identities WHERE code=?", (code,)).fetchone()
        except sqlite3.Error as exc:
            raise RuntimeError("qfq source identity unavailable") from exc
        return str(row[0]) if row is not None else None

    def retain_codes(self, allowed_codes: frozenset[str]) -> tuple[tuple[str, ...], int]:
        """Remove derived windows outside the active published history eligibility."""

        if any(len(code) != 6 or not code.isascii() or not code.isdigit() for code in allowed_codes):
            raise ValueError("qfq allowed code set is invalid")
        changed: list[str] = []
        deleted_rows = 0
        try:
            for name in self._existing_names():
                with closing(self._read_connection(name)) as connection:
                    excluded = tuple(
                        (str(code), int(rows))
                        for code, rows in connection.execute(
                            "SELECT identities.code,COUNT(bars.day) FROM identities "
                            "LEFT JOIN bars ON bars.code=identities.code GROUP BY identities.code"
                        )
                        if code not in allowed_codes
                    )
                if not excluded:
                    continue
                with closing(self._write_connection(name)) as connection, connection:
                    connection.executemany("DELETE FROM bars WHERE code=?", ((code,) for code, _ in excluded))
                    connection.executemany("DELETE FROM identities WHERE code=?", ((code,) for code, _ in excluded))
                changed.append(f"{self.root.name}/{name}")
                deleted_rows += sum(rows for _, rows in excluded)
        except sqlite3.Error as exc:
            raise RuntimeError("qfq eligibility pruning failed") from exc
        return tuple(changed), deleted_rows

    def replace_window(self, window: PublishedHistoryWindow, source_identity: str) -> tuple[tuple[str, ...], int]:
        return self.replace_windows((window,), source_identity)

    def replace_windows(
        self, windows: Sequence[PublishedHistoryWindow], source_identity: str
    ) -> tuple[tuple[str, ...], int]:
        self._ensure_layout()
        try:
            return self._replace_windows(windows, source_identity)
        except sqlite3.Error as exc:
            raise RuntimeError("qfq shard update failed") from exc

    def _replace_windows(
        self, windows: Sequence[PublishedHistoryWindow], source_identity: str
    ) -> tuple[tuple[str, ...], int]:
        grouped: dict[str, list[tuple[str, tuple[tuple[str, str], ...]]]] = {}
        for window in windows:
            cells = window.cells[-self.sessions :]
            if cells:
                grouped.setdefault(qfq_shard_name(window.code), []).append(
                    (window.code, tuple((cell.trade_date.isoformat(), encode_cell(cell)) for cell in cells))
                )
        if not grouped:
            return (), 0
        self.root.mkdir(parents=True, exist_ok=True)
        changed: list[str] = []
        changed_rows = 0
        for name, entries in grouped.items():
            shard_changed = False
            with closing(self._write_connection(name)) as connection, connection:
                for code, target in entries:
                    old = self._read_rows(connection, code)
                    identity = connection.execute("SELECT source FROM identities WHERE code=?", (code,)).fetchone()
                    if old == target and identity is not None and identity[0] == source_identity:
                        continue
                    self._write_code(connection, code, target, source_identity)
                    old_by_day = dict(old)
                    new_by_day = dict(target)
                    changed_rows += sum(
                        old_by_day.get(day) != new_by_day.get(day) for day in old_by_day.keys() | new_by_day.keys()
                    )
                    shard_changed = True
            if shard_changed:
                changed.append(f"{self.root.name}/{name}")
        return tuple(changed), changed_rows

    @staticmethod
    def _write_code(connection: sqlite3.Connection, code: str, rows: tuple[tuple[str, str], ...], source: str) -> None:
        existing = dict(connection.execute("SELECT day,payload FROM bars WHERE code=?", (code,)))
        incoming = dict(rows)
        connection.executemany(
            "DELETE FROM bars WHERE code=? AND day=?", ((code, day) for day in existing.keys() - incoming.keys())
        )
        connection.executemany(
            "INSERT OR REPLACE INTO bars VALUES (?,?,?)",
            ((code, day, payload) for day, payload in rows if existing.get(day) != payload),
        )
        connection.execute(
            "INSERT OR REPLACE INTO identities VALUES (?,?,?,?,?)",
            (code, _digest(rows), source, rows[-1][0], json.dumps([day for day, _ in rows], separators=(",", ":"))),
        )

    def _write_connection(self, name: str) -> sqlite3.Connection:
        connection = sqlite3.connect(self.root / name, timeout=5)
        try:
            connection.execute("PRAGMA page_size=4096")
            connection.execute("PRAGMA journal_mode=DELETE")
            connection.execute("PRAGMA synchronous=FULL")
            if not connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchone():
                connection.executescript(_SCHEMA)
            return connection
        except sqlite3.Error:
            connection.close()
            raise

    def _read_connection(self, name: str) -> sqlite3.Connection:
        path = self.root / name
        if not path.is_file() or path.is_symlink():
            raise RuntimeError("qfq shard missing or unsafe")
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
        identities: list[tuple[str, str, str]] = []
        dates: set[date] = set()
        for name in self._existing_names():
            with closing(self._read_connection(name)) as connection:
                for code, digest, source, calendar_json in connection.execute(
                    "SELECT code,digest,source,dates FROM identities"
                ):
                    if qfq_shard_name(code) != name:
                        raise RuntimeError("qfq code routed to wrong shard")
                    identities.append((code, digest, source))
                    dates.update(date.fromisoformat(day) for day in json.loads(calendar_json))
        if not identities:
            return None
        if not dates:
            raise RuntimeError("qfq shard identity incomplete")
        calendar = tuple(sorted(dates))[-self.sessions :]
        digest = hashlib.sha256(json.dumps(sorted(identities)).encode("ascii")).hexdigest()
        return PublishedHistoryManifest(
            digest, 1, calendar[-1], calendar, tuple(sorted(code for code, _, _ in identities))
        )

    def iter_windows(self, manifest: PublishedHistoryManifest, *, sessions: int) -> Iterator[PublishedHistoryWindow]:
        for name in self._existing_names():
            codes = tuple(code for code in manifest.universe_codes if qfq_shard_name(code) == name)
            yield from self.read_windows(manifest, codes, sessions=sessions)

    def read_windows(
        self, manifest: PublishedHistoryManifest, codes: Sequence[str], *, sessions: int
    ) -> tuple[PublishedHistoryWindow, ...]:
        self._ensure_layout()
        try:
            return self._read_windows(manifest, codes, sessions=sessions)
        except sqlite3.Error as exc:
            raise RuntimeError("qfq windows unavailable") from exc

    def _read_windows(
        self, manifest: PublishedHistoryManifest, codes: Sequence[str], *, sessions: int
    ) -> tuple[PublishedHistoryWindow, ...]:
        if not 1 <= sessions <= self.sessions:
            raise ValueError("qfq read window invalid")
        grouped: dict[str, list[str]] = {}
        allowed = frozenset(manifest.universe_codes)
        for code in dict.fromkeys(codes):
            name = qfq_shard_name(code)
            if code in allowed and (self.root / name).is_file():
                grouped.setdefault(name, []).append(code)
        result: list[PublishedHistoryWindow] = []
        for name, requested in grouped.items():
            with closing(self._read_connection(name)) as connection, connection:
                for code in requested:
                    rows = self._read_rows(connection, code)[-sessions:]
                    result.append(
                        PublishedHistoryWindow(code, tuple(decode_cell(code, day, payload) for day, payload in rows))
                    )
        return tuple(sorted(result, key=lambda item: item.code))
