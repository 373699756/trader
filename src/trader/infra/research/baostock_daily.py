"""BaoStock row gateway and immutable SQLite artifacts for offline research."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import sqlite3
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Protocol, cast

from trader.application.research.baostock_daily import BaoStockShardContext
from trader.domain.research.baostock_daily import (
    BaoStockAdjustment,
    BaoStockBoard,
    BaoStockBoardCoverage,
    BaoStockCalendar,
    BaoStockCellStatus,
    BaoStockCodeBatch,
    BaoStockCodeCoverage,
    BaoStockCoverageAudit,
    BaoStockCoverageStatus,
    BaoStockDailyCell,
    BaoStockDailyManifest,
    BaoStockDailySide,
    BaoStockDailySpec,
    BaoStockSecurity,
    BaoStockSourceVersions,
    BaoStockTradingStatus,
    build_baostock_coverage_audit,
    join_baostock_daily_sides,
)
from trader.domain.research.h1_point_in_time import canonical_hash
from trader.domain.research.tomorrow_v3_input_compatibility import DailyInputField, FrozenDailyInputDescriptor
from trader.infra.research.baostock_daily_codec import (
    boolean as _boolean,
)
from trader.infra.research.baostock_daily_codec import (
    dates as _dates,
)
from trader.infra.research.baostock_daily_codec import (
    encode_json as _json,
)
from trader.infra.research.baostock_daily_codec import (
    fields as _fields,
)
from trader.infra.research.baostock_daily_codec import (
    integer as _integer,
)
from trader.infra.research.baostock_daily_codec import (
    json_array as _json_array,
)
from trader.infra.research.baostock_daily_codec import (
    json_object as _json_object,
)
from trader.infra.research.baostock_daily_codec import (
    number as _number,
)
from trader.infra.research.baostock_daily_codec import (
    object_value as _object,
)
from trader.infra.research.baostock_daily_codec import (
    optional_number as _optional_number,
)
from trader.infra.research.baostock_daily_codec import (
    pairs as _pairs,
)
from trader.infra.research.baostock_daily_codec import (
    string as _string,
)
from trader.infra.research.baostock_daily_codec import (
    strings as _strings,
)

_DAILY_FIELDS = "date,code,open,high,low,close,preclose,volume,amount,adjustflag,turn,tradestatus,pctChg"
_BOARDS: tuple[BaoStockBoard, ...] = ("main", "chinext", "star")
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
    DailyInputField("qfq_open", "cny_per_share_qfq"),
    DailyInputField("qfq_high", "cny_per_share_qfq"),
    DailyInputField("qfq_low", "cny_per_share_qfq"),
    DailyInputField("qfq_close", "cny_per_share_qfq"),
    DailyInputField("qfq_volume", "shares"),
    DailyInputField("qfq_amount", "cny"),
)


class BaoStockDailyArtifactConflictError(RuntimeError):
    """Raised when a shard, merged database, or manifest changes identity."""


class BaoStockRowResult(Protocol):
    error_code: str
    error_msg: str
    fields: Sequence[str]

    def next(self) -> bool: ...

    def get_row_data(self) -> Sequence[str]: ...


class BaoStockSdkPort(Protocol):
    __version__: str

    def query_trade_dates(self, *, start_date: str, end_date: str) -> BaoStockRowResult: ...

    def query_stock_basic(self) -> BaoStockRowResult: ...

    def query_history_k_data_plus(  # noqa: PLR0913 - exact third-party SDK signature
        self,
        code: str,
        fields: str,
        start_date: str,
        end_date: str,
        *,
        frequency: str,
        adjustflag: str,
    ) -> BaoStockRowResult: ...


class BaoStockRowGateway:
    """Translate an already-owned SDK session using only next/get_row_data."""

    def __init__(
        self,
        sdk: BaoStockSdkPort,
        *,
        python_version: str | None = None,
        dependency_versions: tuple[tuple[str, str], ...] = (),
    ) -> None:
        self._sdk = sdk
        self._versions = BaoStockSourceVersions(
            sdk.__version__,
            python_version or platform.python_version(),
            dependency_versions,
        )

    def source_versions(self) -> BaoStockSourceVersions:
        return self._versions

    def fetch_calendar(self, spec: BaoStockDailySpec) -> BaoStockCalendar:
        lookback = spec.source_cutoff - timedelta(days=max(730, spec.sessions * 2))
        result = self._sdk.query_trade_dates(
            start_date=lookback.isoformat(),
            end_date=spec.source_cutoff.isoformat(),
        )
        rows = _result_rows(result, "calendar_query_failed")
        dates = tuple(
            date.fromisoformat(row["calendar_date"])
            for row in rows
            if row.get("is_trading_day") == "1" and row.get("calendar_date")
        )
        if len(dates) < spec.sessions:
            raise ValueError("BaoStock calendar returned fewer sessions than requested")
        return BaoStockCalendar(tuple(sorted(set(dates)))[-spec.sessions :])

    def fetch_universe(self, spec: BaoStockDailySpec) -> tuple[BaoStockSecurity, ...]:
        rows = _result_rows(self._sdk.query_stock_basic(), "security_query_failed")
        securities: list[BaoStockSecurity] = []
        for row in rows:
            if row.get("type") != "1":
                continue
            source_code = row.get("code", "")
            board = _board(source_code)
            if board is None:
                continue
            listed = _date(row.get("ipoDate"), "BaoStock listing date is missing")
            delisted = _optional_date(row.get("outDate"))
            if listed > spec.source_cutoff:
                continue
            securities.append(
                BaoStockSecurity(
                    source_code.split(".")[-1],
                    row.get("code_name", ""),
                    board,
                    listed,
                    delisted,
                    self._versions.sdk_version,
                )
            )
        ordered = tuple(sorted(securities, key=lambda item: item.code))
        if not ordered or len({item.code for item in ordered}) != len(ordered):
            raise ValueError("BaoStock security master is empty or duplicated")
        return ordered

    def fetch_code_batch(
        self,
        spec: BaoStockDailySpec,
        security: BaoStockSecurity,
        calendar: BaoStockCalendar,
    ) -> BaoStockCodeBatch:
        expected = calendar.expected_dates(security)
        if not expected:
            return BaoStockCodeBatch(security.code, ())
        raw, raw_nulls, raw_future = self._daily_sides(spec, security, "unadjusted", "3", expected)
        qfq, qfq_nulls, qfq_future = self._daily_sides(spec, security, "qfq", "2", expected)
        batch = join_baostock_daily_sides(
            security.code,
            expected,
            raw,
            qfq,
            null_rows=raw_nulls + qfq_nulls,
        )
        if raw_future + qfq_future == 0:
            return batch
        return BaoStockCodeBatch(
            batch.code,
            batch.cells,
            batch.duplicate_rows,
            batch.null_rows,
            batch.out_of_window_rows,
            raw_future + qfq_future,
            batch.failure_reasons,
        )

    def _daily_sides(
        self,
        spec: BaoStockDailySpec,
        security: BaoStockSecurity,
        adjustment: BaoStockAdjustment,
        adjustflag: str,
        expected: tuple[date, ...],
    ) -> tuple[tuple[BaoStockDailySide, ...], int, int]:
        result = self._sdk.query_history_k_data_plus(
            security.source_code,
            _DAILY_FIELDS,
            expected[0].isoformat(),
            expected[-1].isoformat(),
            frequency="d",
            adjustflag=adjustflag,
        )
        rows = _result_rows(result, f"{adjustment}_daily_query_failed")
        sides: list[BaoStockDailySide] = []
        null_rows = future_rows = 0
        for row in rows:
            try:
                trade_date = _date(row.get("date"), "BaoStock daily date is missing")
                if trade_date > spec.source_cutoff:
                    future_rows += 1
                    continue
                if row.get("code") != security.source_code or row.get("adjustflag") != adjustflag:
                    raise ValueError("BaoStock daily row identity is invalid")
                side = _daily_side(security.code, trade_date, adjustment, row)
            except (TypeError, ValueError):
                null_rows += 1
                continue
            sides.append(side)
        return tuple(sides), null_rows, future_rows


def _result_rows(result: BaoStockRowResult, error_code: str) -> tuple[dict[str, str], ...]:
    if result.error_code != "0":
        raise RuntimeError(_supplier_failure_code(result.error_code, error_code))
    fields = tuple(result.fields)
    if not fields or len(set(fields)) != len(fields):
        raise ValueError("BaoStock result fields are empty or duplicated")
    rows: list[dict[str, str]] = []
    while result.next():
        values = tuple(result.get_row_data())
        if len(values) != len(fields):
            raise ValueError("BaoStock result row width is invalid")
        rows.append(dict(zip(fields, values, strict=True)))
    if result.error_code != "0":
        raise RuntimeError(_supplier_failure_code(result.error_code, error_code))
    return tuple(rows)


def _supplier_failure_code(vendor_code: str, fallback: str) -> str:
    if vendor_code == "10001011":
        return "supplier_query_failed_blacklisted"
    return fallback


def _daily_side(
    code: str,
    trade_date: date,
    adjustment: BaoStockAdjustment,
    row: dict[str, str],
) -> BaoStockDailySide:
    status = _trading_status(row.get("tradestatus"))
    return BaoStockDailySide(
        code=code,
        trade_date=trade_date,
        adjustment=adjustment,
        open_price=_optional_float(row.get("open")),
        high_price=_optional_float(row.get("high")),
        low_price=_optional_float(row.get("low")),
        close_price=_optional_float(row.get("close")),
        volume=_optional_float(row.get("volume")),
        amount=_optional_float(row.get("amount")),
        preclose=_optional_float(row.get("preclose")) if adjustment == "unadjusted" else None,
        pct_change=_optional_ratio(row.get("pctChg")) if adjustment == "unadjusted" else None,
        turnover=_optional_ratio(row.get("turn")) if adjustment == "unadjusted" else None,
        trading_status=status,
    )


def _trading_status(value: str | None) -> BaoStockTradingStatus:
    if value == "1":
        return "trading"
    if value == "0":
        return "suspended"
    raise ValueError("BaoStock trading status is invalid")


def _optional_float(value: str | None) -> float | None:
    if value is None or not value.strip():
        return None
    return float(value)


def _optional_ratio(value: str | None) -> float | None:
    number = _optional_float(value)
    return None if number is None else number / 100.0


def _date(value: str | None, message: str) -> date:
    if value is None or not value.strip():
        raise ValueError(message)
    return date.fromisoformat(value)


def _optional_date(value: str | None) -> date | None:
    return None if value is None or not value.strip() else date.fromisoformat(value)


def _board(source_code: str) -> BaoStockBoard | None:
    if source_code.startswith("sh.688") or source_code.startswith("sh.689"):
        return "star"
    if source_code.startswith("sz.300") or source_code.startswith("sz.301"):
        return "chinext"
    main_prefixes = ("sh.600", "sh.601", "sh.603", "sh.605", "sz.000", "sz.001", "sz.002", "sz.003")
    return "main" if source_code.startswith(main_prefixes) else None


@dataclass(frozen=True)
class BaoStockShardSnapshot:
    spec: BaoStockDailySpec
    context: BaoStockShardContext
    batches: tuple[BaoStockCodeBatch, ...]
    failures: tuple[tuple[str, str], ...]
    schema_version: str = "baostock_daily_shard_snapshot_v2"
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        batches = tuple(sorted(self.batches, key=lambda item: item.code))
        failures = tuple(sorted(self.failures))
        if len({item.code for item in batches}) != len(batches):
            raise ValueError("BaoStock shard snapshot contains duplicate code batches")
        if len({item[0] for item in failures}) != len(failures):
            raise ValueError("BaoStock shard snapshot contains duplicate failures")
        if self.schema_version != "baostock_daily_shard_snapshot_v2":
            raise ValueError("BaoStock shard snapshot schema is invalid")
        object.__setattr__(self, "batches", batches)
        object.__setattr__(self, "failures", failures)
        object.__setattr__(self, "content_hash", canonical_hash(self))


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
                """
            )

    def context(self, spec: BaoStockDailySpec) -> BaoStockShardContext | None:
        try:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT spec_json, calendar_json, universe_json, versions_json, context_hash "
                    "FROM context WHERE singleton=1"
                ).fetchone()
            if row is None:
                return None
            stored_spec = _decode_spec(_json_object(row[0]))
            calendar = _decode_calendar(_json_object(row[1]))
            universe = _decode_universe(_json_array(row[2]))
            versions = _decode_versions(_json_object(row[3]))
            expected_hash = canonical_hash((stored_spec, calendar, universe, versions))
            if stored_spec.content_hash != spec.content_hash or row[4] != expected_hash:
                raise BaoStockDailyArtifactConflictError("BaoStock shard context identity conflict")
            return BaoStockShardContext(calendar, universe, versions)
        except BaoStockDailyArtifactConflictError:
            raise
        except (KeyError, TypeError, ValueError, json.JSONDecodeError, sqlite3.DatabaseError) as exc:
            raise BaoStockDailyArtifactConflictError("BaoStock shard context payload or hash is invalid") from exc

    def initialize(
        self,
        spec: BaoStockDailySpec,
        calendar: BaoStockCalendar,
        universe: tuple[BaoStockSecurity, ...],
        source_versions: BaoStockSourceVersions,
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
        try:
            with self._connect() as connection:
                connection.execute("INSERT INTO context VALUES (?, ?, ?, ?, ?, ?)", values)
        except sqlite3.IntegrityError:
            existing = self.context(spec)
            if existing != BaoStockShardContext(calendar, ordered_universe, source_versions):
                raise BaoStockDailyArtifactConflictError("BaoStock shard context identity conflict") from None

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


class BaoStockDailyMergedArtifactStore:
    def __init__(self, root: Path) -> None:
        self._root = root
        self._database = root / "score-baostock-daily-core-v2.sqlite3"
        self._manifest = root / "manifest.json"

    def write(
        self,
        spec: BaoStockDailySpec,
        snapshots: tuple[BaoStockShardSnapshot, ...],
    ) -> BaoStockDailyManifest:
        if not snapshots:
            raise ValueError("BaoStock merge requires at least one shard")
        context = _common_context(spec, snapshots)
        batches = _merged_batches(snapshots)
        audit = build_baostock_coverage_audit(spec, context.calendar, context.universe, batches)
        self._root.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix=".baostock-merged.", suffix=".sqlite3", dir=self._root)
        os.close(descriptor)
        temporary = Path(temporary_name)
        temporary.unlink()
        try:
            merged = SQLiteBaoStockDailyShard(temporary)
            merged.initialize(spec, context.calendar, context.universe, context.source_versions)
            for batch in batches:
                merged.save_batch(spec, batch)
            _checkpoint_database(temporary)
            database_hash = _file_sha256(temporary)
            logical_hash = canonical_hash(tuple((item.code, item.content_hash) for item in batches))
            manifest = BaoStockDailyManifest(
                spec_hash=spec.content_hash,
                calendar_hash=context.calendar.content_hash,
                universe_hash=canonical_hash(context.universe),
                logical_records_hash=logical_hash,
                source_versions_hash=context.source_versions.content_hash,
                source_versions=context.source_versions,
                database_sha256=database_hash,
                audit=audit,
            )
            if self._manifest.exists() or self._database.exists():
                existing = self.verify()
                if existing.content_hash != manifest.content_hash:
                    raise BaoStockDailyArtifactConflictError("BaoStock merged artifact identity conflict")
                return existing
            os.link(temporary, self._database)
            _write_immutable_json(self._manifest, _encode_manifest(manifest), manifest.content_hash)
            return self.verify()
        finally:
            temporary.unlink(missing_ok=True)
            temporary.with_name(temporary.name + "-wal").unlink(missing_ok=True)
            temporary.with_name(temporary.name + "-shm").unlink(missing_ok=True)

    def verify(self) -> BaoStockDailyManifest:
        try:
            raw = _json_object(self._manifest.read_text(encoding="utf-8"))
            stored_hash = raw.pop("content_hash")
            if not isinstance(stored_hash, str):
                raise TypeError("BaoStock manifest hash is invalid")
            manifest = _decode_manifest(raw)
            if manifest.content_hash != stored_hash or manifest.database_sha256 != _file_sha256(self._database):
                raise ValueError("BaoStock manifest or database hash mismatch")
            return manifest
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise BaoStockDailyArtifactConflictError("BaoStock merged manifest is invalid") from exc

    def describe_frozen_daily_input(self) -> FrozenDailyInputDescriptor:
        manifest = self.verify()
        try:
            with sqlite3.connect(self._database) as connection:
                row = connection.execute("SELECT spec_json FROM context WHERE singleton=1").fetchone()
            if row is None:
                raise ValueError("BaoStock merged database context is missing")
            spec = _decode_spec(_json_object(row[0]))
            if spec.content_hash != manifest.spec_hash:
                raise ValueError("BaoStock merged spec hash mismatch")
        except (TypeError, ValueError, json.JSONDecodeError, sqlite3.DatabaseError) as exc:
            raise BaoStockDailyArtifactConflictError("BaoStock merged input description is invalid") from exc
        return FrozenDailyInputDescriptor(
            manifest_hash=manifest.content_hash,
            source_identity=spec.research_identity,
            source_cutoff=spec.source_cutoff,
            requested_sessions=spec.sessions,
            primary_key=("code", "trade_date"),
            fields=_FROZEN_DAILY_FIELDS,
            raw_qfq_layout="same_row",
            row_hash_algorithm="sha256",
            frozen=True,
            production_authority=False,
        )

    def read_cells(self, code: str) -> tuple[BaoStockDailyCell, ...]:
        self.verify()
        with sqlite3.connect(self._database) as connection:
            rows = connection.execute(
                "SELECT payload_json, content_hash FROM daily_cells WHERE code=? ORDER BY trade_date", (code,)
            ).fetchall()
        cells = tuple(_decode_cell(_json_object(payload)) for payload, _ in rows)
        if any(cell.content_hash != stored_hash for cell, (_, stored_hash) in zip(cells, rows, strict=True)):
            raise BaoStockDailyArtifactConflictError("BaoStock merged daily cell hash is invalid")
        return cells


def _common_context(spec: BaoStockDailySpec, snapshots: tuple[BaoStockShardSnapshot, ...]) -> BaoStockShardContext:
    first = snapshots[0].context
    for snapshot in snapshots:
        if snapshot.spec.content_hash != spec.content_hash or snapshot.context != first:
            raise BaoStockDailyArtifactConflictError("BaoStock shard contexts do not match")
    return first


def _merged_batches(snapshots: tuple[BaoStockShardSnapshot, ...]) -> tuple[BaoStockCodeBatch, ...]:
    batches: dict[str, BaoStockCodeBatch] = {}
    for snapshot in snapshots:
        for batch in snapshot.batches:
            previous = batches.get(batch.code)
            if previous is not None and previous.content_hash != batch.content_hash:
                raise BaoStockDailyArtifactConflictError("BaoStock duplicate shard code identity conflict")
            batches[batch.code] = batch
    return tuple(sorted(batches.values(), key=lambda item: item.code))


def _checkpoint_database(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def _write_immutable_json(path: Path, payload: dict[str, object], content_hash: str) -> None:
    document = dict(payload)
    document["content_hash"] = content_hash
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(_json(document))
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, path)
    except FileExistsError:
        raise BaoStockDailyArtifactConflictError("BaoStock merged manifest identity conflict") from None
    finally:
        temporary.unlink(missing_ok=True)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _valid_error_code(value: str) -> bool:
    return (
        0 < len(value) <= 64 and value.isascii() and all(character.isalnum() or character == "_" for character in value)
    )


def _encode_spec(value: BaoStockDailySpec) -> dict[str, object]:
    return {
        "sessions": value.sessions,
        "research_identity": value.research_identity,
        "source_cutoff": value.source_cutoff.isoformat(),
        "production_authority": value.production_authority,
        "point_in_time_parity": value.point_in_time_parity,
        "schema_version": value.schema_version,
    }


def _decode_spec(raw: dict[str, object]) -> BaoStockDailySpec:
    _fields(
        raw,
        {
            "sessions",
            "research_identity",
            "source_cutoff",
            "production_authority",
            "point_in_time_parity",
            "schema_version",
        },
        "spec",
    )
    return BaoStockDailySpec(
        sessions=_integer(raw["sessions"]),
        research_identity=_string(raw["research_identity"]),
        source_cutoff=date.fromisoformat(_string(raw["source_cutoff"])),
        production_authority=_boolean(raw["production_authority"]),
        point_in_time_parity=_boolean(raw["point_in_time_parity"]),
        schema_version=_string(raw["schema_version"]),
    )


def _encode_calendar(value: BaoStockCalendar) -> dict[str, object]:
    return {
        "open_dates": [item.isoformat() for item in value.open_dates],
        "schema_version": value.schema_version,
    }


def _decode_calendar(raw: dict[str, object]) -> BaoStockCalendar:
    _fields(raw, {"open_dates", "schema_version"}, "calendar")
    return BaoStockCalendar(_dates(raw["open_dates"]), schema_version=_string(raw["schema_version"]))


def _encode_security(value: BaoStockSecurity) -> dict[str, object]:
    return {
        "code": value.code,
        "name": value.name,
        "board": value.board,
        "listed_on": value.listed_on.isoformat(),
        "delisted_on": value.delisted_on.isoformat() if value.delisted_on is not None else None,
        "source_version": value.source_version,
    }


def _decode_security(raw: dict[str, object]) -> BaoStockSecurity:
    _fields(raw, {"code", "name", "board", "listed_on", "delisted_on", "source_version"}, "security")
    delisted = raw["delisted_on"]
    if delisted is not None and not isinstance(delisted, str):
        raise TypeError("BaoStock security delisted_on is invalid")
    return BaoStockSecurity(
        _string(raw["code"]),
        _string(raw["name"]),
        cast(BaoStockBoard, _string(raw["board"])),
        date.fromisoformat(_string(raw["listed_on"])),
        date.fromisoformat(delisted) if delisted is not None else None,
        _string(raw["source_version"]),
    )


def _decode_universe(raw: list[object]) -> tuple[BaoStockSecurity, ...]:
    return tuple(_decode_security(_object(item, "security")) for item in raw)


def _encode_versions(value: BaoStockSourceVersions) -> dict[str, object]:
    return {
        "sdk_version": value.sdk_version,
        "python_version": value.python_version,
        "dependency_versions": [list(item) for item in value.dependency_versions],
    }


def _decode_versions(raw: dict[str, object]) -> BaoStockSourceVersions:
    _fields(raw, {"sdk_version", "python_version", "dependency_versions"}, "versions")
    return BaoStockSourceVersions(
        _string(raw["sdk_version"]),
        _string(raw["python_version"]),
        _pairs(raw["dependency_versions"]),
    )


def _encode_side(value: BaoStockDailySide) -> dict[str, object]:
    return {
        "code": value.code,
        "trade_date": value.trade_date.isoformat(),
        "adjustment": value.adjustment,
        "open_price": value.open_price,
        "high_price": value.high_price,
        "low_price": value.low_price,
        "close_price": value.close_price,
        "volume": value.volume,
        "amount": value.amount,
        "preclose": value.preclose,
        "pct_change": value.pct_change,
        "turnover": value.turnover,
        "trading_status": value.trading_status,
    }


def _decode_side(raw: dict[str, object]) -> BaoStockDailySide:
    expected = {
        "code",
        "trade_date",
        "adjustment",
        "open_price",
        "high_price",
        "low_price",
        "close_price",
        "volume",
        "amount",
        "preclose",
        "pct_change",
        "turnover",
        "trading_status",
    }
    _fields(raw, expected, "daily side")
    return BaoStockDailySide(
        _string(raw["code"]),
        date.fromisoformat(_string(raw["trade_date"])),
        cast(BaoStockAdjustment, _string(raw["adjustment"])),
        _optional_number(raw["open_price"]),
        _optional_number(raw["high_price"]),
        _optional_number(raw["low_price"]),
        _optional_number(raw["close_price"]),
        _optional_number(raw["volume"]),
        _optional_number(raw["amount"]),
        _optional_number(raw["preclose"]),
        _optional_number(raw["pct_change"]),
        _optional_number(raw["turnover"]),
        cast(BaoStockTradingStatus, _string(raw["trading_status"])),
    )


def _encode_cell(value: BaoStockDailyCell) -> dict[str, object]:
    return {
        "code": value.code,
        "trade_date": value.trade_date.isoformat(),
        "status": value.status,
        "unadjusted": _encode_side(value.unadjusted) if value.unadjusted is not None else None,
        "qfq": _encode_side(value.qfq) if value.qfq is not None else None,
    }


def _decode_cell(raw: dict[str, object]) -> BaoStockDailyCell:
    _fields(raw, {"code", "trade_date", "status", "unadjusted", "qfq"}, "daily cell")
    unadjusted = raw["unadjusted"]
    qfq = raw["qfq"]
    return BaoStockDailyCell(
        _string(raw["code"]),
        date.fromisoformat(_string(raw["trade_date"])),
        cast(BaoStockCellStatus, _string(raw["status"])),
        _decode_side(_object(unadjusted, "unadjusted")) if unadjusted is not None else None,
        _decode_side(_object(qfq, "qfq")) if qfq is not None else None,
    )


def _encode_batch_metadata(value: BaoStockCodeBatch) -> dict[str, object]:
    return {
        "duplicate_rows": value.duplicate_rows,
        "null_rows": value.null_rows,
        "out_of_window_rows": value.out_of_window_rows,
        "future_rows": value.future_rows,
        "failure_reasons": list(value.failure_reasons),
    }


def _decode_batch_metadata(
    code: str,
    raw: dict[str, object],
    cells: tuple[BaoStockDailyCell, ...],
) -> BaoStockCodeBatch:
    _fields(
        raw,
        {"duplicate_rows", "null_rows", "out_of_window_rows", "future_rows", "failure_reasons"},
        "batch metadata",
    )
    return BaoStockCodeBatch(
        code,
        cells,
        _integer(raw["duplicate_rows"]),
        _integer(raw["null_rows"]),
        _integer(raw["out_of_window_rows"]),
        _integer(raw["future_rows"]),
        _strings(raw["failure_reasons"]),
    )


def _encode_audit(value: BaoStockCoverageAudit) -> dict[str, object]:
    return {
        "spec_hash": value.spec_hash,
        "calendar_hash": value.calendar_hash,
        "universe_hash": value.universe_hash,
        "calendar_sessions": value.calendar_sessions,
        "calendar_first_date": value.calendar_first_date.isoformat(),
        "calendar_last_date": value.calendar_last_date.isoformat(),
        "universe_count": value.universe_count,
        "expected_cells": value.expected_cells,
        "obtained_cells": value.obtained_cells,
        "all_cell_coverage": value.all_cell_coverage,
        "board_coverages": [
            {
                "board": item.board,
                "expected_cells": item.expected_cells,
                "obtained_cells": item.obtained_cells,
                "coverage_ratio": item.coverage_ratio,
            }
            for item in value.board_coverages
        ],
        "code_coverages": [
            {
                "code": item.code,
                "expected_cells": item.expected_cells,
                "obtained_cells": item.obtained_cells,
                "coverage_ratio": item.coverage_ratio,
                "eligible_for_v3_population": item.eligible_for_v3_population,
            }
            for item in value.code_coverages
        ],
        "full_window_stock_count": value.full_window_stock_count,
        "full_window_stocks_at_95_percent": value.full_window_stocks_at_95_percent,
        "full_window_stock_success_ratio": value.full_window_stock_success_ratio,
        "failed_codes": list(value.failed_codes),
        "duplicate_rows": value.duplicate_rows,
        "null_rows": value.null_rows,
        "out_of_window_rows": value.out_of_window_rows,
        "future_rows": value.future_rows,
        "latest_reserved_dates": [item.isoformat() for item in value.latest_reserved_dates],
        "status": value.status,
        "failure_reasons": list(value.failure_reasons),
        "terminal_holdout_opened": value.terminal_holdout_opened,
        "production_authority": value.production_authority,
        "point_in_time_parity": value.point_in_time_parity,
        "schema_version": value.schema_version,
    }


def _decode_audit(raw: dict[str, object]) -> BaoStockCoverageAudit:
    expected = {
        "spec_hash",
        "calendar_hash",
        "universe_hash",
        "calendar_sessions",
        "calendar_first_date",
        "calendar_last_date",
        "universe_count",
        "expected_cells",
        "obtained_cells",
        "all_cell_coverage",
        "board_coverages",
        "code_coverages",
        "full_window_stock_count",
        "full_window_stocks_at_95_percent",
        "full_window_stock_success_ratio",
        "failed_codes",
        "duplicate_rows",
        "null_rows",
        "out_of_window_rows",
        "future_rows",
        "latest_reserved_dates",
        "status",
        "failure_reasons",
        "terminal_holdout_opened",
        "production_authority",
        "point_in_time_parity",
        "schema_version",
    }
    _fields(raw, expected, "coverage audit")
    return BaoStockCoverageAudit(
        spec_hash=_string(raw["spec_hash"]),
        calendar_hash=_string(raw["calendar_hash"]),
        universe_hash=_string(raw["universe_hash"]),
        calendar_sessions=_integer(raw["calendar_sessions"]),
        calendar_first_date=date.fromisoformat(_string(raw["calendar_first_date"])),
        calendar_last_date=date.fromisoformat(_string(raw["calendar_last_date"])),
        universe_count=_integer(raw["universe_count"]),
        expected_cells=_integer(raw["expected_cells"]),
        obtained_cells=_integer(raw["obtained_cells"]),
        all_cell_coverage=_number(raw["all_cell_coverage"]),
        board_coverages=_decode_board_coverages(raw["board_coverages"]),
        code_coverages=_decode_code_coverages(raw["code_coverages"]),
        full_window_stock_count=_integer(raw["full_window_stock_count"]),
        full_window_stocks_at_95_percent=_integer(raw["full_window_stocks_at_95_percent"]),
        full_window_stock_success_ratio=_number(raw["full_window_stock_success_ratio"]),
        failed_codes=_strings(raw["failed_codes"]),
        duplicate_rows=_integer(raw["duplicate_rows"]),
        null_rows=_integer(raw["null_rows"]),
        out_of_window_rows=_integer(raw["out_of_window_rows"]),
        future_rows=_integer(raw["future_rows"]),
        latest_reserved_dates=_dates(raw["latest_reserved_dates"]),
        status=cast(BaoStockCoverageStatus, _string(raw["status"])),
        failure_reasons=_strings(raw["failure_reasons"]),
        terminal_holdout_opened=_boolean(raw["terminal_holdout_opened"]),
        production_authority=_boolean(raw["production_authority"]),
        point_in_time_parity=_boolean(raw["point_in_time_parity"]),
        schema_version=_string(raw["schema_version"]),
    )


def _encode_manifest(value: BaoStockDailyManifest) -> dict[str, object]:
    return {
        "spec_hash": value.spec_hash,
        "calendar_hash": value.calendar_hash,
        "universe_hash": value.universe_hash,
        "logical_records_hash": value.logical_records_hash,
        "source_versions_hash": value.source_versions_hash,
        "source_versions": _encode_versions(value.source_versions),
        "database_sha256": value.database_sha256,
        "audit": _encode_audit(value.audit),
        "production_authority": value.production_authority,
        "point_in_time_parity": value.point_in_time_parity,
        "terminal_holdout_opened": value.terminal_holdout_opened,
        "schema_version": value.schema_version,
    }


def _decode_manifest(raw: dict[str, object]) -> BaoStockDailyManifest:
    expected = {
        "spec_hash",
        "calendar_hash",
        "universe_hash",
        "logical_records_hash",
        "source_versions_hash",
        "source_versions",
        "database_sha256",
        "audit",
        "production_authority",
        "point_in_time_parity",
        "terminal_holdout_opened",
        "schema_version",
    }
    _fields(raw, expected, "manifest")
    return BaoStockDailyManifest(
        spec_hash=_string(raw["spec_hash"]),
        calendar_hash=_string(raw["calendar_hash"]),
        universe_hash=_string(raw["universe_hash"]),
        logical_records_hash=_string(raw["logical_records_hash"]),
        source_versions_hash=_string(raw["source_versions_hash"]),
        source_versions=_decode_versions(_object(raw["source_versions"], "source versions")),
        database_sha256=_string(raw["database_sha256"]),
        audit=_decode_audit(_object(raw["audit"], "audit")),
        production_authority=_boolean(raw["production_authority"]),
        point_in_time_parity=_boolean(raw["point_in_time_parity"]),
        terminal_holdout_opened=_boolean(raw["terminal_holdout_opened"]),
        schema_version=_string(raw["schema_version"]),
    )


def _decode_board_coverages(value: object) -> tuple[BaoStockBoardCoverage, ...]:
    if not isinstance(value, list):
        raise TypeError("BaoStock board coverage list is invalid")
    results: list[BaoStockBoardCoverage] = []
    for item in value:
        raw = _object(item, "board coverage")
        _fields(raw, {"board", "expected_cells", "obtained_cells", "coverage_ratio"}, "board coverage")
        results.append(
            BaoStockBoardCoverage(
                cast(BaoStockBoard, _string(raw["board"])),
                _integer(raw["expected_cells"]),
                _integer(raw["obtained_cells"]),
                _number(raw["coverage_ratio"]),
            )
        )
    return tuple(results)


def _decode_code_coverages(value: object) -> tuple[BaoStockCodeCoverage, ...]:
    if not isinstance(value, list):
        raise TypeError("BaoStock code coverage list is invalid")
    results: list[BaoStockCodeCoverage] = []
    for item in value:
        raw = _object(item, "code coverage")
        _fields(
            raw,
            {"code", "expected_cells", "obtained_cells", "coverage_ratio", "eligible_for_v3_population"},
            "code coverage",
        )
        results.append(
            BaoStockCodeCoverage(
                _string(raw["code"]),
                _integer(raw["expected_cells"]),
                _integer(raw["obtained_cells"]),
                _number(raw["coverage_ratio"]),
                _boolean(raw["eligible_for_v3_population"]),
            )
        )
    return tuple(results)


__all__ = [
    "BaoStockDailyArtifactConflictError",
    "BaoStockDailyMergedArtifactStore",
    "BaoStockRowGateway",
    "BaoStockRowResult",
    "BaoStockSdkPort",
    "BaoStockShardSnapshot",
    "SQLiteBaoStockDailyShard",
]
