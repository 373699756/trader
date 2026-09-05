"""Immutable BaoStock SQLite shard artifacts for offline research."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import cast

from trader.application.research.baostock_daily import BaoStockShardContext
from trader.domain.research.baostock_daily import (
    BAOSTOCK_DAILY_FACT_SCHEMA,
    BAOSTOCK_INDUSTRY_INTERVAL_SCHEMA,
    BAOSTOCK_LEGACY_DAILY_FACT_SCHEMA,
    BAOSTOCK_LEGACY_INDUSTRY_INTERVAL_SCHEMA,
    BaoStockBoard,
    BaoStockCalendar,
    BaoStockCodeBatch,
    BaoStockDailyCell,
    BaoStockDailyFact,
    BaoStockDailySpec,
    BaoStockIndustryInterval,
    BaoStockSecurity,
    BaoStockSourceVersions,
    BaoStockTrainingRow,
)
from trader.domain.research.h1_point_in_time import canonical_hash
from trader.domain.research.tomorrow_v3_input_compatibility import DailyInputField
from trader.infra.research.baostock_daily_codec import encode_json as _json
from trader.infra.research.baostock_daily_codec import json_array as _json_array
from trader.infra.research.baostock_daily_codec import json_object as _json_object
from trader.infra.research.baostock_daily_serialization import (
    _decode_batch_metadata,
    _decode_calendar,
    _decode_cell,
    _decode_spec,
    _decode_universe,
    _decode_versions,
    _encode_batch_metadata,
    _encode_calendar,
    _encode_cell,
    _encode_security,
    _encode_spec,
    _encode_versions,
)

_BOARDS: tuple[BaoStockBoard, ...] = ("main", "chinext", "star")
BAOSTOCK_SHARD_SNAPSHOT_SCHEMA = "baostock_daily_shard_snapshot"
BAOSTOCK_LEGACY_SHARD_SNAPSHOT_SCHEMA = "baostock_daily_shard_snapshot_v2"
_FROZEN_DAILY_FIELDS = (
    DailyInputField("code", "security_code"),
    DailyInputField("trade_date", "iso_date"),
    DailyInputField("board", "board_id"),
    DailyInputField("raw_open", "cny_per_share"),
    DailyInputField("raw_high", "cny_per_share"),
    DailyInputField("raw_low", "cny_per_share"),
    DailyInputField("raw_close", "cny_per_share"),
    DailyInputField("raw_pre_close", "cny_per_share"),
    DailyInputField("raw_volume", "shares"),
    DailyInputField("raw_amount", "cny"),
    DailyInputField("raw_pct_change", "ratio"),
    DailyInputField("raw_turnover_rate", "ratio"),
    DailyInputField("trade_status", "supplier_trade_status"),
    DailyInputField("is_st", "boolean"),
    DailyInputField("industry", "baostock_industry"),
    DailyInputField("qfq_open", "cny_per_share_qfq"),
    DailyInputField("qfq_high", "cny_per_share_qfq"),
    DailyInputField("qfq_low", "cny_per_share_qfq"),
    DailyInputField("qfq_close", "cny_per_share_qfq"),
    DailyInputField("qfq_volume", "shares"),
    DailyInputField("qfq_amount", "cny"),
)


class BaoStockDailyArtifactConflictError(RuntimeError):
    """Raised when a shard, merged database, or manifest changes identity."""


@dataclass(frozen=True)
class BaoStockShardSnapshot:
    spec: BaoStockDailySpec
    context: BaoStockShardContext
    batches: tuple[BaoStockCodeBatch, ...]
    failures: tuple[tuple[str, str], ...]
    schema_version: str = BAOSTOCK_SHARD_SNAPSHOT_SCHEMA
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        batches = tuple(sorted(self.batches, key=lambda item: item.code))
        failures = tuple(sorted(self.failures))
        if len({item.code for item in batches}) != len(batches):
            raise ValueError("BaoStock shard snapshot contains duplicate code batches")
        if len({item[0] for item in failures}) != len(failures):
            raise ValueError("BaoStock shard snapshot contains duplicate failures")
        if self.schema_version not in (BAOSTOCK_SHARD_SNAPSHOT_SCHEMA, BAOSTOCK_LEGACY_SHARD_SNAPSHOT_SCHEMA):
            raise ValueError("BaoStock shard snapshot schema is invalid")
        object.__setattr__(self, "batches", batches)
        object.__setattr__(self, "failures", failures)
        object.__setattr__(self, "content_hash", canonical_hash(self))


@dataclass(frozen=True)
class BaoStockShardCheckpoint:
    """Small, index-only view used to resume a download.

    The checkpoint path must never deserialize daily payloads.  A resume run
    only needs the durable code state and the number of rows already stored;
    the expensive cell/hash decode remains an explicit final-merge operation.
    """

    completed_codes: frozenset[str]
    ready_codes: frozenset[str]
    failures: tuple[tuple[str, str], ...]
    downloaded_records: int

    def __post_init__(self) -> None:
        if self.downloaded_records < 0:
            raise ValueError("BaoStock checkpoint record count must be non-negative")
        completed = frozenset(self.completed_codes)
        ready = frozenset(self.ready_codes)
        failures = tuple(sorted(self.failures))
        if not ready.issubset(completed):
            raise ValueError("BaoStock ready codes must be completed")
        if set(code for code, _ in failures) & completed:
            raise ValueError("BaoStock completed codes cannot also be failed")
        object.__setattr__(self, "completed_codes", completed)
        object.__setattr__(self, "ready_codes", ready)
        object.__setattr__(self, "failures", failures)


class SQLiteBaoStockDailyShard:
    def __init__(self, path: Path) -> None:
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._create_schema()

    @property
    def path(self) -> Path:
        return self._path

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _create_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS context (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    spec_json TEXT NOT NULL,
                    calendar_json TEXT NOT NULL,
                    universe_json TEXT NOT NULL,
                    versions_json TEXT NOT NULL,
                    context_hash TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS daily_cells (
                    code TEXT NOT NULL,
                    trade_date TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    PRIMARY KEY (code, trade_date)
                );
                CREATE TABLE IF NOT EXISTS code_batches (
                    code TEXT PRIMARY KEY,
                    metadata_json TEXT NOT NULL,
                    content_hash TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS checkpoints (
                    code TEXT PRIMARY KEY,
                    state TEXT NOT NULL,
                    error_code TEXT,
                    batch_hash TEXT
                );
                CREATE TABLE IF NOT EXISTS daily_facts (
                    code TEXT NOT NULL,
                    trade_date TEXT NOT NULL,
                    is_st INTEGER NOT NULL CHECK (is_st IN (0, 1)),
                    content_hash TEXT NOT NULL,
                    PRIMARY KEY (code, trade_date)
                );
                CREATE TABLE IF NOT EXISTS industry_intervals (
                    code TEXT NOT NULL,
                    effective_from TEXT NOT NULL,
                    effective_to TEXT,
                    industry TEXT NOT NULL,
                    classification TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    PRIMARY KEY (code, effective_from)
                );
                CREATE TABLE IF NOT EXISTS training_fact_checkpoints (
                    code TEXT PRIMARY KEY,
                    content_hash TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS daily_cells_trade_date_idx ON daily_cells(trade_date, code);
                CREATE INDEX IF NOT EXISTS daily_facts_trade_date_idx ON daily_facts(trade_date, code);
                """
            )

    def context(self, spec: BaoStockDailySpec) -> BaoStockShardContext | None:
        try:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT spec_json, calendar_json, universe_json, versions_json, context_hash "
                    "FROM context WHERE singleton=1"
                ).fetchone()
                industry_rows = connection.execute(
                    "SELECT code, effective_from, effective_to, industry, classification, content_hash "
                    "FROM industry_intervals ORDER BY code, effective_from"
                ).fetchall()
            if row is None:
                return None
            stored_spec = _decode_spec(_json_object(row[0]))
            calendar = _decode_calendar(_json_object(row[1]))
            universe = _decode_universe(_json_array(row[2]))
            versions = _decode_versions(_json_object(row[3]))
            expected_hash = canonical_hash((stored_spec, calendar, universe, versions))
            if stored_spec.content_hash != spec.content_hash or row[4] != expected_hash:
                raise BaoStockDailyArtifactConflictError("BaoStock shard context identity conflict")
            intervals = _decode_all_industry_intervals(industry_rows)
            return BaoStockShardContext(calendar, universe, versions, intervals)
        except BaoStockDailyArtifactConflictError:
            raise
        except (KeyError, TypeError, ValueError, json.JSONDecodeError, sqlite3.DatabaseError) as exc:
            raise BaoStockDailyArtifactConflictError("BaoStock shard context payload or hash is invalid") from exc

    def context_matches(self, spec: BaoStockDailySpec, context: BaoStockShardContext) -> bool:
        """Check a shard's frozen context using its stored identity only.

        Resume startup has one context per partition, but all partitions repeat
        the same frozen universe and industry facts.  Decoding those large JSON
        blobs for every shard is unnecessary; the full payload is still decoded
        by ``context``/``snapshot`` at the explicit validation boundaries.
        """
        expected_hash = canonical_hash((spec, context.calendar, context.universe, context.source_versions))
        try:
            with self._connect() as connection:
                row = connection.execute("SELECT context_hash FROM context WHERE singleton=1").fetchone()
            return row is not None and row[0] == expected_hash
        except sqlite3.DatabaseError as exc:
            raise BaoStockDailyArtifactConflictError("BaoStock shard context identity is unreadable") from exc

    def initialize(
        self,
        spec: BaoStockDailySpec,
        calendar: BaoStockCalendar,
        universe: tuple[BaoStockSecurity, ...],
        source_versions: BaoStockSourceVersions,
        industry_intervals: tuple[BaoStockIndustryInterval, ...] = (),
    ) -> None:
        ordered_universe = tuple(sorted(universe, key=lambda item: item.code))
        if len(calendar.open_dates) != spec.sessions:
            raise ValueError("BaoStock shard calendar does not match requested sessions")
        context_hash = canonical_hash((spec, calendar, ordered_universe, source_versions))
        values = (
            1,
            _json(_encode_spec(spec)),
            _json(_encode_calendar(calendar)),
            _json([_encode_security(item) for item in ordered_universe]),
            _json(_encode_versions(source_versions)),
            context_hash,
        )
        inserted_context = True
        try:
            with self._connect() as connection:
                connection.execute("INSERT INTO context VALUES (?, ?, ?, ?, ?, ?)", values)
        except sqlite3.IntegrityError:
            inserted_context = False
            expected_context = BaoStockShardContext(
                calendar, ordered_universe, source_versions, tuple(industry_intervals)
            )
            if not self.context_matches(spec, expected_context):
                raise BaoStockDailyArtifactConflictError("BaoStock shard context identity conflict") from None
        if industry_intervals and (inserted_context or not self._has_industry_intervals()):
            with self._connect() as connection:
                connection.executemany(
                    "INSERT OR IGNORE INTO industry_intervals VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        (
                            item.code,
                            item.effective_from.isoformat(),
                            item.effective_to.isoformat() if item.effective_to is not None else None,
                            item.industry,
                            item.classification,
                            item.content_hash,
                        )
                        for item in industry_intervals
                    ),
                )

    def _has_industry_intervals(self) -> bool:
        with self._connect() as connection:
            row = connection.execute("SELECT 1 FROM industry_intervals LIMIT 1").fetchone()
        return row is not None

    def completed_codes(self, spec: BaoStockDailySpec) -> frozenset[str]:
        self._require_context(spec)
        with self._connect() as connection:
            rows = connection.execute("SELECT code FROM checkpoints WHERE state='completed' ORDER BY code").fetchall()
        return frozenset(cast(str, row[0]) for row in rows)

    def save_batch(self, spec: BaoStockDailySpec, batch: BaoStockCodeBatch) -> None:
        context = self._require_context(spec)
        securities = {item.code: item for item in context.universe}
        if batch.code not in securities:
            raise ValueError("BaoStock batch code is outside the registered universe")
        expected_dates = context.calendar.expected_dates(securities[batch.code])
        if tuple(item.trade_date for item in batch.cells) != expected_dates:
            raise ValueError("BaoStock batch cells do not cover the registered expected dates")
        metadata = _encode_batch_metadata(batch)
        try:
            with self._connect() as connection:
                existing = connection.execute(
                    "SELECT content_hash FROM code_batches WHERE code=?", (batch.code,)
                ).fetchone()
                if existing is not None:
                    if existing[0] != batch.content_hash:
                        raise BaoStockDailyArtifactConflictError("BaoStock code batch identity conflict")
                    return
                for cell in batch.cells:
                    connection.execute(
                        "INSERT INTO daily_cells VALUES (?, ?, ?, ?)",
                        (cell.code, cell.trade_date.isoformat(), _json(_encode_cell(cell)), cell.content_hash),
                    )
                connection.execute(
                    "INSERT INTO code_batches VALUES (?, ?, ?)",
                    (batch.code, _json(metadata), batch.content_hash),
                )
                connection.execute(
                    "INSERT INTO checkpoints(code, state, error_code, batch_hash) VALUES (?, 'completed', NULL, ?) "
                    "ON CONFLICT(code) DO UPDATE SET state='completed', error_code=NULL, batch_hash=excluded.batch_hash",
                    (batch.code, batch.content_hash),
                )
        except sqlite3.IntegrityError as exc:
            raise BaoStockDailyArtifactConflictError("BaoStock shard SQLite identity conflict") from exc

    def save_training_facts(
        self,
        spec: BaoStockDailySpec,
        code: str,
        facts: tuple[BaoStockDailyFact, ...],
        intervals: tuple[BaoStockIndustryInterval, ...],
    ) -> None:
        context = self._require_context(spec)
        security = next((item for item in context.universe if item.code == code), None)
        if security is None:
            raise ValueError("BaoStock training facts code is outside the registered universe")
        expected_dates = context.calendar.expected_dates(security)
        ordered_facts = tuple(sorted(facts, key=lambda item: item.trade_date))
        ordered_intervals = tuple(sorted(intervals, key=lambda item: item.effective_from))
        if tuple(item.trade_date for item in ordered_facts) != expected_dates or any(
            item.code != code for item in ordered_facts
        ):
            raise ValueError("BaoStock daily facts do not cover the registered expected dates")
        if not ordered_intervals or any(item.code != code for item in ordered_intervals):
            raise ValueError("BaoStock historical industry is missing")
        if any(_industry_for_date(ordered_intervals, day) is None for day in expected_dates):
            raise ValueError("BaoStock historical industry does not cover every expected date")
        frozen_intervals = tuple(item for item in context.industry_intervals if item.code == code)
        if ordered_intervals != frozen_intervals:
            raise BaoStockDailyArtifactConflictError("BaoStock training industry differs from frozen context")
        content_hash = canonical_hash((ordered_facts, ordered_intervals))
        try:
            with self._connect() as connection:
                existing = connection.execute(
                    "SELECT content_hash FROM training_fact_checkpoints WHERE code=?", (code,)
                ).fetchone()
                if existing is not None:
                    if existing[0] != content_hash:
                        raise BaoStockDailyArtifactConflictError("BaoStock training facts identity conflict")
                    return
                connection.executemany(
                    "INSERT INTO daily_facts VALUES (?, ?, ?, ?)",
                    (
                        (item.code, item.trade_date.isoformat(), int(item.is_st), item.content_hash)
                        for item in ordered_facts
                    ),
                )
                connection.execute("INSERT INTO training_fact_checkpoints VALUES (?, ?)", (code, content_hash))
        except sqlite3.IntegrityError as exc:
            raise BaoStockDailyArtifactConflictError("BaoStock training facts SQLite identity conflict") from exc

    def training_ready_codes(self, spec: BaoStockDailySpec) -> frozenset[str]:
        self._require_context(spec)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT c.code FROM checkpoints c JOIN training_fact_checkpoints f ON f.code=c.code "
                "WHERE c.state='completed' ORDER BY c.code"
            ).fetchall()
        return frozenset(cast(str, row[0]) for row in rows)

    def checkpoint(
        self,
        spec: BaoStockDailySpec,
        *,
        expected_records_by_code: Mapping[str, int] | None = None,
    ) -> BaoStockShardCheckpoint:
        """Read the durable resume index without decoding any daily payload.

        ``daily_cells.payload_json`` can be gigabytes large.  It is deliberately
        absent from this query: the checkpoint tables already identify the
        completed/failed codes, and the caller can derive their expected row
        counts from the frozen calendar.
        """
        if expected_records_by_code is None:
            context = self._require_context(spec)
            expected_by_code = {item.code: len(context.calendar.expected_dates(item)) for item in context.universe}
        else:
            expected_by_code = dict(expected_records_by_code)
        try:
            with self._connect() as connection:
                completed_rows = connection.execute(
                    "SELECT code FROM checkpoints WHERE state='completed' ORDER BY code"
                ).fetchall()
                ready_rows = connection.execute(
                    "SELECT c.code FROM checkpoints c "
                    "JOIN training_fact_checkpoints f ON f.code=c.code "
                    "WHERE c.state='completed' ORDER BY c.code"
                ).fetchall()
                failure_rows = connection.execute(
                    "SELECT code, error_code FROM checkpoints WHERE state='failed' ORDER BY code"
                ).fetchall()
            completed = frozenset(cast(str, row[0]) for row in completed_rows)
            ready = frozenset(cast(str, row[0]) for row in ready_rows)
            failures = tuple((cast(str, code), cast(str, reason)) for code, reason in failure_rows)
            if not completed.issubset(expected_by_code) or not ready.issubset(completed):
                raise BaoStockDailyArtifactConflictError("BaoStock checkpoint code is outside the frozen universe")
            return BaoStockShardCheckpoint(
                completed,
                ready,
                failures,
                sum(expected_by_code[code] for code in completed),
            )
        except BaoStockDailyArtifactConflictError:
            raise
        except (TypeError, ValueError, sqlite3.DatabaseError) as exc:
            raise BaoStockDailyArtifactConflictError("BaoStock checkpoint index is invalid") from exc

    def read_training_rows(
        self,
        spec: BaoStockDailySpec,
        code: str,
        *,
        allowed_dates: frozenset[date],
    ) -> tuple[BaoStockTrainingRow, ...]:
        context = self._require_context(spec)
        security = next((item for item in context.universe if item.code == code), None)
        if security is None or code not in self.training_ready_codes(spec):
            raise BaoStockDailyArtifactConflictError("BaoStock code is not training-ready")
        with self._connect() as connection:
            cell_rows = connection.execute(
                "SELECT trade_date, payload_json, content_hash FROM daily_cells WHERE code=? ORDER BY trade_date",
                (code,),
            ).fetchall()
            fact_rows = connection.execute(
                "SELECT trade_date, is_st, content_hash FROM daily_facts WHERE code=? ORDER BY trade_date", (code,)
            ).fetchall()
            industry_rows = connection.execute(
                "SELECT effective_from, effective_to, industry, classification, content_hash "
                "FROM industry_intervals WHERE code=? ORDER BY effective_from",
                (code,),
            ).fetchall()
        facts = _decode_daily_facts(code, fact_rows)
        intervals = _decode_industry_intervals(code, industry_rows)
        rows: list[BaoStockTrainingRow] = []
        for trade_date, payload, stored_hash in cell_rows:
            day = date.fromisoformat(cast(str, trade_date))
            if day not in allowed_dates:
                continue
            cell = _decode_cell(_json_object(cast(str, payload)))
            if cell.content_hash != stored_hash or cell.status != "complete":
                continue
            fact = facts.get(day)
            industry = _industry_for_date(intervals, day)
            if fact is None or industry is None or cell.unadjusted is None or cell.qfq is None:
                raise BaoStockDailyArtifactConflictError("BaoStock training facts are incomplete")
            rows.append(
                BaoStockTrainingRow(
                    code,
                    day,
                    security.board,
                    industry.industry,
                    fact.is_st,
                    cell.unadjusted,
                    cell.qfq,
                )
            )
        return tuple(rows)

    def training_facts_hash(self, spec: BaoStockDailySpec) -> str:
        self._require_context(spec)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT code, content_hash FROM training_fact_checkpoints ORDER BY code"
            ).fetchall()
        return canonical_hash(tuple((cast(str, code), cast(str, value)) for code, value in rows))

    def record_failure(self, spec: BaoStockDailySpec, code: str, error_code: str) -> None:
        context = self._require_context(spec)
        if code not in {item.code for item in context.universe} or not _valid_error_code(error_code):
            raise ValueError("BaoStock checkpoint failure identity is invalid")
        with self._connect() as connection:
            existing = connection.execute("SELECT state FROM checkpoints WHERE code=?", (code,)).fetchone()
            if existing is not None and existing[0] == "completed":
                return
            connection.execute(
                "INSERT INTO checkpoints(code, state, error_code, batch_hash) VALUES (?, 'failed', ?, NULL) "
                "ON CONFLICT(code) DO UPDATE SET state='failed', error_code=excluded.error_code, batch_hash=NULL",
                (code, error_code),
            )

    def clear_failure(self, spec: BaoStockDailySpec, code: str) -> None:
        context = self._require_context(spec)
        if code not in {item.code for item in context.universe}:
            raise ValueError("BaoStock checkpoint code is outside the registered universe")
        with self._connect() as connection:
            connection.execute("DELETE FROM checkpoints WHERE code=? AND state='failed'", (code,))

    def snapshot(self, spec: BaoStockDailySpec) -> BaoStockShardSnapshot:
        try:
            return self._snapshot(spec)
        except BaoStockDailyArtifactConflictError:
            raise
        except (KeyError, TypeError, ValueError, json.JSONDecodeError, sqlite3.DatabaseError) as exc:
            raise BaoStockDailyArtifactConflictError("BaoStock shard payload or hash is invalid") from exc

    def _snapshot(self, spec: BaoStockDailySpec) -> BaoStockShardSnapshot:
        context = self._require_context(spec)
        with self._connect() as connection:
            batch_rows = connection.execute(
                "SELECT code, metadata_json, content_hash FROM code_batches ORDER BY code"
            ).fetchall()
            cell_rows = connection.execute(
                "SELECT code, trade_date, payload_json, content_hash FROM daily_cells ORDER BY code, trade_date"
            ).fetchall()
            checkpoints = connection.execute(
                "SELECT code, state, error_code, batch_hash FROM checkpoints ORDER BY code"
            ).fetchall()
        cells_by_code = _decode_cell_rows(cell_rows)
        batches = _decode_batch_rows(batch_rows, cells_by_code)
        failures = _decode_checkpoint_rows(checkpoints, context, batches)
        return BaoStockShardSnapshot(
            spec,
            context,
            batches,
            failures,
        )

    def _require_context(self, spec: BaoStockDailySpec) -> BaoStockShardContext:
        context = self.context(spec)
        if context is None:
            raise BaoStockDailyArtifactConflictError("BaoStock shard context is missing")
        return context


def _decode_cell_rows(rows: Sequence[Sequence[object]]) -> dict[str, list[BaoStockDailyCell]]:
    cells_by_code: dict[str, list[BaoStockDailyCell]] = {}
    for code, trade_date, payload_json, stored_hash in rows:
        cell = _decode_cell(_json_object(cast(str, payload_json)))
        if cell.code != code or cell.trade_date.isoformat() != trade_date or cell.content_hash != stored_hash:
            raise BaoStockDailyArtifactConflictError("BaoStock daily cell payload or hash is invalid")
        if not isinstance(code, str):
            raise BaoStockDailyArtifactConflictError("BaoStock daily cell code is invalid")
        cells_by_code.setdefault(code, []).append(cell)
    return cells_by_code


def _decode_batch_rows(
    rows: Sequence[Sequence[object]],
    cells_by_code: dict[str, list[BaoStockDailyCell]],
) -> tuple[BaoStockCodeBatch, ...]:
    batches: list[BaoStockCodeBatch] = []
    for code, metadata_json, stored_hash in rows:
        batch = _decode_batch_metadata(
            cast(str, code),
            _json_object(cast(str, metadata_json)),
            tuple(cells_by_code.pop(cast(str, code), ())),
        )
        if batch.content_hash != stored_hash:
            raise BaoStockDailyArtifactConflictError("BaoStock code batch payload or hash is invalid")
        batches.append(batch)
    if cells_by_code:
        raise BaoStockDailyArtifactConflictError("BaoStock shard contains orphan daily cells")
    return tuple(batches)


def _decode_checkpoint_rows(
    rows: Sequence[Sequence[object]],
    context: BaoStockShardContext,
    batches: tuple[BaoStockCodeBatch, ...],
) -> tuple[tuple[str, str], ...]:
    batch_hashes = {item.code: item.content_hash for item in batches}
    failures: list[tuple[str, str]] = []
    seen: set[str] = set()
    universe_codes = {item.code for item in context.universe}
    for code, state, error_code, batch_hash in rows:
        code_value = cast(str, code)
        if code_value in seen or code_value not in universe_codes:
            raise BaoStockDailyArtifactConflictError("BaoStock shard checkpoint identity is invalid")
        seen.add(code_value)
        if state == "completed":
            if error_code is not None or batch_hashes.get(code_value) != batch_hash:
                raise BaoStockDailyArtifactConflictError("BaoStock shard checkpoint hash is invalid")
        elif state == "failed":
            if not isinstance(error_code, str) or not _valid_error_code(error_code) or batch_hash is not None:
                raise BaoStockDailyArtifactConflictError("BaoStock shard failed checkpoint is invalid")
            failures.append((code_value, error_code))
        else:
            raise BaoStockDailyArtifactConflictError("BaoStock shard checkpoint state is invalid")
    if seen.intersection(batch_hashes) != set(batch_hashes):
        raise BaoStockDailyArtifactConflictError("BaoStock shard completed checkpoint is missing")
    return tuple(failures)


def _decode_daily_facts(
    code: str,
    rows: Sequence[Sequence[object]],
) -> dict[date, BaoStockDailyFact]:
    result: dict[date, BaoStockDailyFact] = {}
    for trade_date, is_st, stored_hash in rows:
        day = date.fromisoformat(cast(str, trade_date))
        candidates = tuple(
            BaoStockDailyFact(code, day, bool(is_st), schema_version=schema)
            for schema in (BAOSTOCK_DAILY_FACT_SCHEMA, BAOSTOCK_LEGACY_DAILY_FACT_SCHEMA)
        )
        matching = tuple(item for item in candidates if item.content_hash == stored_hash)
        if len(matching) != 1 or day in result:
            raise BaoStockDailyArtifactConflictError("BaoStock daily fact payload or hash is invalid")
        result[day] = matching[0]
    return result


def _decode_industry_intervals(
    code: str,
    rows: Sequence[Sequence[object]],
) -> tuple[BaoStockIndustryInterval, ...]:
    result: list[BaoStockIndustryInterval] = []
    for effective_from, effective_to, industry, classification, stored_hash in rows:
        start = date.fromisoformat(cast(str, effective_from))
        end = date.fromisoformat(cast(str, effective_to)) if effective_to is not None else None
        candidates = tuple(
            BaoStockIndustryInterval(
                code,
                start,
                end,
                cast(str, industry),
                cast(str, classification),
                schema_version=schema,
            )
            for schema in (BAOSTOCK_INDUSTRY_INTERVAL_SCHEMA, BAOSTOCK_LEGACY_INDUSTRY_INTERVAL_SCHEMA)
        )
        matching = tuple(item for item in candidates if item.content_hash == stored_hash)
        if len(matching) != 1:
            raise BaoStockDailyArtifactConflictError("BaoStock industry interval payload or hash is invalid")
        result.append(matching[0])
    ordered = tuple(result)
    if any(left.effective_to != right.effective_from for left, right in zip(ordered, ordered[1:], strict=False)):
        raise BaoStockDailyArtifactConflictError("BaoStock industry intervals are not contiguous")
    return ordered


def _decode_all_industry_intervals(
    rows: Sequence[Sequence[object]],
) -> tuple[BaoStockIndustryInterval, ...]:
    grouped: dict[str, list[Sequence[object]]] = {}
    for row in rows:
        grouped.setdefault(cast(str, row[0]), []).append(row[1:])
    result: list[BaoStockIndustryInterval] = []
    for code, values in grouped.items():
        result.extend(_decode_industry_intervals(code, values))
    return tuple(sorted(result, key=lambda item: (item.code, item.effective_from)))


def _industry_for_date(
    intervals: tuple[BaoStockIndustryInterval, ...],
    day: date,
) -> BaoStockIndustryInterval | None:
    return next(
        (
            item
            for item in intervals
            if item.effective_from <= day and (item.effective_to is None or day < item.effective_to)
        ),
        None,
    )


def industry_covers_expected_dates(security: BaoStockSecurity, context: BaoStockShardContext) -> bool:
    intervals = tuple(item for item in context.industry_intervals if item.code == security.code)
    return bool(intervals) and all(
        any(item.effective_from <= day and (item.effective_to is None or day < item.effective_to) for item in intervals)
        for day in context.calendar.expected_dates(security)
    )


def _valid_error_code(value: str) -> bool:
    return (
        0 < len(value) <= 64 and value.isascii() and all(character.isalnum() or character == "_" for character in value)
    )


from trader.infra.research.baostock_gateway import (  # noqa: E402
    BaoStockRowGateway,
    BaoStockRowResult,
    BaoStockSdkPort,
)
from trader.infra.research.baostock_partition_archive import BaoStockDailyPartitionedArchive  # noqa: E402

__all__ = [
    "BAOSTOCK_LEGACY_SHARD_SNAPSHOT_SCHEMA",
    "BAOSTOCK_SHARD_SNAPSHOT_SCHEMA",
    "BaoStockDailyArtifactConflictError",
    "BaoStockDailyPartitionedArchive",
    "BaoStockRowGateway",
    "BaoStockRowResult",
    "BaoStockSdkPort",
    "BaoStockShardSnapshot",
    "SQLiteBaoStockDailyShard",
    "industry_covers_expected_dates",
]
