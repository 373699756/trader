"""Atomic SQLite storage for the independent H1 point-in-time archive."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
from typing import cast
from zoneinfo import ZoneInfo

from trader.application.research.h1_point_in_time import H1ArchivePort
from trader.application.research.historical_screening import HistoricalSecurity, ResearchBoard
from trader.domain.research.h1_point_in_time import (
    H1CoverageAudit,
    H1CoverageManifest,
    H1CoverageState,
    H1PointInTimeRecord,
    H1PointInTimeSpec,
    H1Strategy,
    canonical_hash,
)
from trader.domain.research.historical_label import H1CoverageMetadata


class H1ArchiveConflictError(RuntimeError):
    pass


@dataclass(frozen=True)
class H1ArchiveStatus:
    initialized: bool = False
    strategy: H1Strategy = "today"
    universe_count: int = 0
    completed_codes: int = 0
    failed_codes: int = 0
    record_count: int = 0
    first_trade_date: str | None = None
    last_trade_date: str | None = None
    spec_hash: str = ""


class SQLiteH1PointInTimeArchive(H1ArchivePort):
    def __init__(self, runtime_dir: Path) -> None:
        self._root = runtime_dir / "score-h1-point-in-time"
        self._database = self._root / "score-h1-point-in-time.sqlite3"
        self._lock = threading.RLock()

    def _initialize(self) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        with self._write_connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS specs (
                    strategy TEXT PRIMARY KEY, research_identity TEXT NOT NULL,
                    spec_hash TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS universe (
                    strategy TEXT NOT NULL, code TEXT NOT NULL, board TEXT NOT NULL,
                    name TEXT NOT NULL, is_st INTEGER NOT NULL, is_suspended INTEGER NOT NULL,
                    payload_hash TEXT NOT NULL, PRIMARY KEY(strategy, code)
                );
                CREATE TABLE IF NOT EXISTS records (
                    strategy TEXT NOT NULL, code TEXT NOT NULL, trade_date TEXT NOT NULL,
                    observed_at TEXT NOT NULL, open_price REAL NOT NULL, close_price REAL NOT NULL,
                    high_price REAL NOT NULL, low_price REAL NOT NULL, volume REAL NOT NULL,
                    amount REAL NOT NULL, pct_change REAL NOT NULL, turnover_rate REAL,
                    adjustment TEXT NOT NULL, source TEXT NOT NULL, anchor_price REAL NOT NULL,
                    anchor_volume REAL NOT NULL, anchor_amount REAL NOT NULL,
                    security_state_hash TEXT NOT NULL, sector_hash TEXT NOT NULL,
                    risk_facts_hash TEXT NOT NULL, tail_field_hash TEXT NOT NULL,
                    payload_hash TEXT NOT NULL, PRIMARY KEY(strategy, code, trade_date)
                );
                CREATE TABLE IF NOT EXISTS downloads (
                    strategy TEXT NOT NULL, code TEXT NOT NULL, status TEXT NOT NULL,
                    record_count INTEGER NOT NULL, content_hash TEXT NOT NULL,
                    error_code TEXT NOT NULL, PRIMARY KEY(strategy, code)
                );
                """
            )

    def register_universe(self, spec: H1PointInTimeSpec, universe: Sequence[HistoricalSecurity]) -> None:
        self._initialize()
        ordered = tuple(sorted(universe, key=lambda item: item.code))
        if len({item.code for item in ordered}) != len(ordered):
            raise ValueError("H1 universe contains duplicate codes")
        with self._write_connection() as connection:
            self._register_spec(connection, spec)
            existing = tuple(
                str(row[0])
                for row in connection.execute(
                    "SELECT code FROM universe WHERE strategy = ? ORDER BY code", (spec.strategy,)
                ).fetchall()
            )
            requested = tuple(item.code for item in ordered)
            if existing and existing != requested:
                raise H1ArchiveConflictError("H1 universe set conflict")
            for item in ordered:
                payload_hash = canonical_hash(item)
                prior = connection.execute(
                    "SELECT payload_hash FROM universe WHERE strategy = ? AND code = ?",
                    (spec.strategy, item.code),
                ).fetchone()
                if prior is not None and str(prior[0]) != payload_hash:
                    raise H1ArchiveConflictError("H1 universe identity conflict")
                connection.execute(
                    "INSERT OR IGNORE INTO universe(strategy, code, board, name, is_st, is_suspended, payload_hash) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        spec.strategy,
                        item.code,
                        item.board,
                        item.name,
                        int(item.is_st),
                        int(item.is_suspended),
                        payload_hash,
                    ),
                )

    def registered_universe(self, strategy: H1Strategy) -> tuple[HistoricalSecurity, ...]:
        if not self._database.is_file():
            return ()
        with self._read_connection() as connection:
            rows = connection.execute(
                "SELECT code, board, name, is_st, is_suspended FROM universe WHERE strategy = ? ORDER BY code",
                (strategy,),
            ).fetchall()
        return tuple(
            HistoricalSecurity(str(code), cast(ResearchBoard, str(board)), str(name), bool(is_st), bool(is_suspended))
            for code, board, name, is_st, is_suspended in rows
        )

    def completed_codes(self, strategy: H1Strategy) -> frozenset[str]:
        if not self._database.is_file():
            return frozenset()
        with self._read_connection() as connection:
            rows = connection.execute(
                "SELECT code FROM downloads WHERE strategy = ? AND status = 'complete'", (strategy,)
            ).fetchall()
        return frozenset(str(row[0]) for row in rows)

    def save_records(self, spec: H1PointInTimeSpec, code: str, records: Sequence[H1PointInTimeRecord]) -> None:
        self._initialize()
        ordered = tuple(records)
        if not ordered or len(ordered) > spec.max_history_sessions:
            raise ValueError("H1 record set exceeds its bounded coverage")
        dates = tuple(item.trade_date for item in ordered)
        if dates != tuple(sorted(set(dates))):
            raise ValueError("H1 record dates must be unique and ordered")
        if any(item.code != code or item.strategy != spec.strategy for item in ordered):
            raise ValueError("H1 record set identity mismatch")
        with self._write_connection() as connection:
            self._register_spec(connection, spec)
            if (
                connection.execute(
                    "SELECT 1 FROM universe WHERE strategy = ? AND code = ?", (spec.strategy, code)
                ).fetchone()
                is None
            ):
                raise ValueError("H1 code is outside the registered universe")
            hashes: list[str] = []
            for record in ordered:
                payload = _record_payload(record)
                payload_hash = canonical_hash(payload)
                hashes.append(payload_hash)
                prior = connection.execute(
                    "SELECT payload_hash FROM records WHERE strategy = ? AND code = ? AND trade_date = ?",
                    (spec.strategy, code, record.trade_date.isoformat()),
                ).fetchone()
                if prior is not None and str(prior[0]) != payload_hash:
                    raise H1ArchiveConflictError("H1 record identity conflict")
                connection.execute(
                    """INSERT OR IGNORE INTO records(strategy, code, trade_date, observed_at, open_price, close_price, high_price, low_price, volume, amount, pct_change, turnover_rate, adjustment, source, anchor_price, anchor_volume, anchor_amount, security_state_hash, sector_hash, risk_facts_hash, tail_field_hash, payload_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        spec.strategy,
                        code,
                        record.trade_date.isoformat(),
                        record.observed_at.isoformat(),
                        record.daily_bar.open_price,
                        record.daily_bar.close,
                        record.daily_bar.high,
                        record.daily_bar.low,
                        record.daily_bar.volume,
                        record.daily_bar.amount,
                        record.daily_bar.pct_change,
                        record.daily_bar.turnover_rate,
                        record.daily_bar.adjustment,
                        record.daily_bar.source,
                        record.anchor_price,
                        record.anchor_volume,
                        record.anchor_amount,
                        record.security_state_hash,
                        record.sector_hash,
                        record.risk_facts_hash,
                        record.tail_field_hash,
                        payload_hash,
                    ),
                )
            content_hash = canonical_hash({"code": code, "records": hashes})
            prior_download = connection.execute(
                "SELECT content_hash FROM downloads WHERE strategy = ? AND code = ? AND status = 'complete'",
                (spec.strategy, code),
            ).fetchone()
            if prior_download is not None and str(prior_download[0]) != content_hash:
                raise H1ArchiveConflictError("H1 download identity conflict")
            connection.execute(
                "INSERT INTO downloads(strategy, code, status, record_count, content_hash, error_code) VALUES (?, ?, 'complete', ?, ?, '') ON CONFLICT(strategy, code) DO UPDATE SET status = excluded.status, record_count = excluded.record_count, content_hash = excluded.content_hash, error_code = excluded.error_code",
                (spec.strategy, code, len(ordered), content_hash),
            )

    def record_failure(self, spec: H1PointInTimeSpec, code: str, error_code: str) -> None:
        if not error_code or len(error_code) > 64:
            raise ValueError("H1 failure code is invalid")
        self._initialize()
        with self._write_connection() as connection:
            self._register_spec(connection, spec)
            connection.execute(
                "INSERT INTO downloads(strategy, code, status, record_count, content_hash, error_code) VALUES (?, ?, 'failed', 0, '', ?) ON CONFLICT(strategy, code) DO UPDATE SET status = CASE WHEN downloads.status = 'complete' THEN downloads.status ELSE excluded.status END, error_code = CASE WHEN downloads.status = 'complete' THEN downloads.error_code ELSE excluded.error_code END",
                (spec.strategy, code, error_code),
            )

    def audit(self, spec: H1PointInTimeSpec) -> H1CoverageAudit:
        manifest = self.manifest(spec)
        ratio = manifest.completed_codes / manifest.universe_count if manifest.universe_count else 0.0
        return H1CoverageAudit(spec.strategy, manifest, ratio)

    def label_metadata(self, spec: H1PointInTimeSpec) -> H1CoverageMetadata:
        """Project date identities for preregistration without reading market values."""

        manifest = self.manifest(spec)
        if not self._database.is_file():
            return H1CoverageMetadata(
                spec.strategy,
                manifest.state,
                (),
                manifest.universe_hash,
                manifest.content_hash,
                spec.source_cutoff,
            )
        with self._read_connection() as connection:
            completed = tuple(
                str(row[0])
                for row in connection.execute(
                    "SELECT code FROM downloads WHERE strategy = ? AND status = 'complete' ORDER BY code",
                    (spec.strategy,),
                ).fetchall()
            )
            rows = connection.execute(
                "SELECT code, trade_date FROM records WHERE strategy = ? ORDER BY code, trade_date",
                (spec.strategy,),
            ).fetchall()
        per_code: dict[str, set[str]] = {code: set() for code in completed}
        for code, trade_date in rows:
            if str(code) in per_code:
                per_code[str(code)].add(str(trade_date))
        date_sets = tuple(per_code[code] for code in completed)
        common = set.intersection(*date_sets) if date_sets else set()
        return H1CoverageMetadata(
            spec.strategy,
            manifest.state,
            tuple(date.fromisoformat(value) for value in sorted(common)),
            manifest.universe_hash,
            manifest.content_hash,
            spec.source_cutoff,
        )

    def manifest(self, spec: H1PointInTimeSpec) -> H1CoverageManifest:
        empty = canonical_hash(())
        if not self._database.is_file():
            return H1CoverageManifest(
                spec.content_hash, empty, empty, empty, empty, empty, 0, 0, 0, 0, "historical_data_insufficient"
            )
        with self._read_connection() as connection:
            stored = connection.execute(
                "SELECT research_identity, spec_hash FROM specs WHERE strategy = ?", (spec.strategy,)
            ).fetchone()
            if stored is None or str(stored[1]) != spec.content_hash or str(stored[0]) != spec.research_identity:
                raise H1ArchiveConflictError("H1 spec manifest conflict")
            universe_rows = connection.execute(
                "SELECT code, board, name, is_st, is_suspended, payload_hash FROM universe WHERE strategy = ? ORDER BY code",
                (spec.strategy,),
            ).fetchall()
            universe_identities: list[tuple[str, str]] = []
            for code, board, name, is_st, is_suspended, stored_hash in universe_rows:
                security = HistoricalSecurity(
                    str(code), cast(ResearchBoard, str(board)), str(name), bool(is_st), bool(is_suspended)
                )
                payload_hash = canonical_hash(security)
                if payload_hash != str(stored_hash):
                    raise H1ArchiveConflictError("H1 universe payload conflict")
                universe_identities.append((security.code, payload_hash))
            universe_hash = canonical_hash(tuple(universe_identities))
            completed_rows = connection.execute(
                "SELECT code, record_count, content_hash FROM downloads WHERE strategy = ? AND status = 'complete' ORDER BY code",
                (spec.strategy,),
            ).fetchall()
            history_hashes = tuple((str(row[0]), int(row[1]), str(row[2])) for row in completed_rows)
            histories_hash = canonical_hash(history_hashes)
            rows = connection.execute(
                "SELECT strategy, code, trade_date, observed_at, open_price, close_price, high_price, low_price, volume, amount, pct_change, turnover_rate, adjustment, source, anchor_price, anchor_volume, anchor_amount, security_state_hash, sector_hash, risk_facts_hash, tail_field_hash, payload_hash FROM records WHERE strategy = ? ORDER BY code, trade_date",
                (spec.strategy,),
            ).fetchall()
        per_code: dict[str, list[str]] = {}
        per_code_days: dict[str, set[str]] = {}
        field_values: list[tuple[object, ...]] = []
        source_values: list[tuple[str, str, str]] = []
        for (
            strategy,
            code,
            trade_date,
            observed_at,
            open_price,
            close_price,
            high_price,
            low_price,
            volume,
            amount,
            pct_change,
            turnover_rate,
            adjustment,
            source,
            anchor_price,
            anchor_volume,
            anchor_amount,
            state_hash,
            sector_hash,
            risk_hash,
            tail_hash,
            payload_hash,
        ) in rows:
            per_code.setdefault(str(code), []).append(str(payload_hash))
            per_code_days.setdefault(str(code), set()).add(str(trade_date))
            if str(adjustment) != "qfq":
                raise H1ArchiveConflictError("H1 non-qfq record detected")
            observed = datetime.fromisoformat(str(observed_at))
            if observed.tzinfo is None:
                raise H1ArchiveConflictError("H1 timezone evidence missing")
            local_observed = observed.astimezone(ZoneInfo("Asia/Shanghai"))
            expected_hour, expected_minute = (11, 20) if spec.strategy == "today" else (14, 50)
            if str(trade_date) > spec.source_cutoff.isoformat() or local_observed.timetz().replace(tzinfo=None) != time(
                expected_hour, expected_minute
            ):
                raise H1ArchiveConflictError("H1 point-in-time cutoff conflict")
            payload = {
                "strategy": str(strategy),
                "code": str(code),
                "trade_date": str(trade_date),
                "observed_at": str(observed_at),
                "open_price": float(open_price),
                "close": float(close_price),
                "high": float(high_price),
                "low": float(low_price),
                "volume": float(volume),
                "amount": float(amount),
                "pct_change": float(pct_change),
                "turnover_rate": float(turnover_rate) if turnover_rate is not None else None,
                "adjustment": str(adjustment),
                "source": str(source),
                "anchor_price": float(anchor_price),
                "anchor_volume": float(anchor_volume),
                "anchor_amount": float(anchor_amount),
                "security_state_hash": str(state_hash),
                "sector_hash": str(sector_hash),
                "risk_facts_hash": str(risk_hash),
                "tail_field_hash": str(tail_hash),
            }
            if canonical_hash(payload) != str(payload_hash):
                raise H1ArchiveConflictError("H1 record payload conflict")
            field_values.append((str(code), str(trade_date), str(state_hash), str(sector_hash), str(risk_hash)))
            source_values.append((str(code), str(trade_date), str(source)))
        for code, count, content_hash in history_hashes:
            hashes = per_code.get(code, [])
            if len(hashes) != count or canonical_hash({"code": code, "records": hashes}) != content_hash:
                raise H1ArchiveConflictError("H1 record content conflict")
        completed_codes = tuple(code for code, _count, _hash in history_hashes)
        date_sets = [per_code_days[code] for code in completed_codes if code in per_code_days]
        common_days = len(set.intersection(*date_sets)) if date_sets else 0
        universe_count = len(universe_rows)
        completed_count = len(completed_rows)
        terminal = min(spec.terminal_holdout_days, common_days)
        state: str = (
            "coverage_ready"
            if completed_count / universe_count >= spec.minimum_coverage_ratio
            and common_days >= spec.minimum_common_days
            and terminal >= spec.terminal_holdout_days
            and rows
            else "historical_data_insufficient"
        )
        calendar_values = tuple(sorted(set.union(*date_sets))) if date_sets else ()
        return H1CoverageManifest(
            spec.content_hash,
            universe_hash,
            histories_hash,
            canonical_hash(calendar_values),
            canonical_hash(tuple(field_values)),
            canonical_hash(tuple(source_values)),
            completed_count,
            universe_count,
            common_days,
            terminal,
            cast(H1CoverageState, state),
        )

    def inspect(self, spec: H1PointInTimeSpec) -> H1ArchiveStatus:
        if not self._database.is_file():
            return H1ArchiveStatus(strategy=spec.strategy)
        with self._read_connection() as connection:
            spec_row = connection.execute("SELECT spec_hash FROM specs WHERE strategy = ?", (spec.strategy,)).fetchone()
            universe_count = int(
                connection.execute("SELECT COUNT(*) FROM universe WHERE strategy = ?", (spec.strategy,)).fetchone()[0]
            )
            completed, failed = connection.execute(
                "SELECT SUM(CASE WHEN status = 'complete' THEN 1 ELSE 0 END), SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) FROM downloads WHERE strategy = ?",
                (spec.strategy,),
            ).fetchone()
            count, first_date, last_date = connection.execute(
                "SELECT COUNT(*), MIN(trade_date), MAX(trade_date) FROM records WHERE strategy = ?", (spec.strategy,)
            ).fetchone()
        return H1ArchiveStatus(
            True,
            spec.strategy,
            universe_count,
            int(completed or 0),
            int(failed or 0),
            int(count or 0),
            str(first_date) if first_date else None,
            str(last_date) if last_date else None,
            str(spec_row[0]) if spec_row else "",
        )

    def _register_spec(self, connection: sqlite3.Connection, spec: H1PointInTimeSpec) -> None:
        prior = connection.execute(
            "SELECT research_identity, spec_hash FROM specs WHERE strategy = ?", (spec.strategy,)
        ).fetchone()
        if prior is not None and (str(prior[0]) != spec.research_identity or str(prior[1]) != spec.content_hash):
            raise H1ArchiveConflictError("H1 spec identity conflict")
        connection.execute(
            "INSERT OR IGNORE INTO specs(strategy, research_identity, spec_hash) VALUES (?, ?, ?)",
            (spec.strategy, spec.research_identity, spec.content_hash),
        )

    def _write_connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database, timeout=30, isolation_level="IMMEDIATE")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _read_connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database)
        connection.execute("PRAGMA query_only = ON")
        return connection


def _record_payload(record: H1PointInTimeRecord) -> dict[str, object]:
    bar = record.daily_bar
    return {
        "strategy": record.strategy,
        "code": record.code,
        "trade_date": record.trade_date.isoformat(),
        "observed_at": record.observed_at.isoformat(),
        "open_price": float(bar.open_price),
        "close": float(bar.close),
        "high": float(bar.high),
        "low": float(bar.low),
        "volume": float(bar.volume),
        "amount": float(bar.amount),
        "pct_change": float(bar.pct_change),
        "turnover_rate": float(bar.turnover_rate) if bar.turnover_rate is not None else None,
        "adjustment": bar.adjustment,
        "source": bar.source,
        "anchor_price": float(record.anchor_price),
        "anchor_volume": float(record.anchor_volume),
        "anchor_amount": float(record.anchor_amount),
        "security_state_hash": record.security_state_hash,
        "sector_hash": record.sector_hash,
        "risk_facts_hash": record.risk_facts_hash,
        "tail_field_hash": record.tail_field_hash,
    }


__all__ = ["H1ArchiveConflictError", "H1ArchiveStatus", "SQLiteH1PointInTimeArchive"]
