"""Read-only verification of published history and durable download receipts."""

from __future__ import annotations

import argparse
from calendar import monthrange
import hashlib
import json
import math
import os
import signal
import stat
import sqlite3
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from itertools import groupby
from pathlib import Path
from typing import cast
from zoneinfo import ZoneInfo

from trader.download.infra.history_control_index import HistoryControlIndex
from trader.download.infra.history_prices import HistoryPricePartition
from trader.download.infra.history_sync_failure import HistoryFailureDetail
from trader.infra.serialization.canonical_codec import CanonicalDigest
from trader.infra.sqlite_codec import DecodeDate, DecodeHash
from trader.infra.sqlite_connection import (
    CheckSqliteIntegrity,
    SqliteConnectionOptions,
    SqliteSession,
    SqliteTransaction,
)
from trader.infra.sqlite_policy import SqliteIoPolicy
from trader.infra.sqlite_publication import SqliteFileStamp, SqlitePublicationError, VerifySqliteFile
from trader.download.infra.price_columns import PRICE_COLUMNS, DecodePriceValues, PriceValues
from trader.download.domain.history_control import HistoryCalendarIdentity, HistorySecurityBoard, HistorySecurityIdentity, HistorySnapshot, HistorySnapshotPartition, HistoryUniverseIdentity
from scripts.core.paths import PROJECT_ROOT, ExternalPath

from scripts.core.reporting import emit_report

_MAX_AUDIT_STOCKS = 12000
_MAX_AUDIT_DATES = 20000
_MAX_LATEST_ROWS = 3_200_000
_MAX_CHECKPOINT_BYTES = 32 * 1024 * 1024
_NUMBER_NAMES = tuple(f"{side}_{name}" for side in ("raw", "qfq") for name in (
    "open", "high", "low", "close", "volume", "amount", "preclose", "change_pct", "turnover_pct",
))
_NUMBER_POSITIONS = (*range(2, 11), *range(13, 22))


class PriceAuditInterrupted(RuntimeError):
    """Only this audit's deadline or cancellation interrupted a bounded operation."""


@dataclass
class PriceAuditBudget:
    seconds: float
    cancelled: Callable[[], bool] = lambda: False
    clock: Callable[[], float] = time.monotonic
    phase: str = "metadata"
    interrupted: str | None = None
    deadline: float = field(init=False)

    def __post_init__(self) -> None:
        if not math.isfinite(self.seconds) or not 0 < self.seconds <= 7200:
            raise ValueError("price_audit_budget_invalid")
        self.deadline = self.clock() + self.seconds

    def check(self) -> None:
        if self.cancelled():
            self.interrupted = "cancelled"
        elif self.clock() >= self.deadline:
            self.interrupted = "budget_exhausted"
        if self.interrupted is not None:
            raise PriceAuditInterrupted(self.interrupted)

    def _progress(self) -> int:
        try:
            self.check()
        except PriceAuditInterrupted:
            return 1
        return 0

    @contextmanager
    def connection(self, path: Path, policy: SqliteIoPolicy) -> Iterator[sqlite3.Connection]:
        self.check()
        _require_regular(path)
        timeout = min(policy.connection_timeout_seconds, max(0.001, self.deadline - self.clock()))
        wal = Path(str(path) + "-wal")
        if wal.is_symlink() or wal.exists() and not wal.is_file():
            raise ValueError("price_audit_source_path_unsafe")
        immutable = not wal.exists() or wal.stat().st_size == 0
        connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro" + ("&immutable=1" if immutable else ""), uri=True, timeout=timeout)
        try:
            connection.execute("PRAGMA query_only=ON")
            connection.set_progress_handler(self._progress, 10000)
            connection.execute("BEGIN")
            yield connection
            self.check()
        except sqlite3.OperationalError as exc:
            if self.interrupted is not None and "interrupt" in str(exc).lower():
                raise PriceAuditInterrupted(self.interrupted) from exc
            raise
        finally:
            connection.close()


@dataclass(frozen=True)
class PriceAuditSecurity:
    code: str
    listed: int
    delisted: int | None

    def __post_init__(self) -> None:
        if (
            len(self.code) != 6 or not self.code.isascii() or not self.code.isdecimal()
            or type(self.listed) is not int
            or self.delisted is not None and (type(self.delisted) is not int or self.delisted <= self.listed)
        ):
            raise ValueError("price_audit_reference_security_invalid")
        DecodeDate(self.listed)
        if self.delisted is not None:
            DecodeDate(self.delisted)


@dataclass(frozen=True)
class PriceAuditSlice:
    first: int
    last: int
    sequence: int


@dataclass(frozen=True)
class PriceAuditUnit:
    name: str
    path: Path
    dates: tuple[int, ...]
    securities: tuple[PriceAuditSecurity, ...]
    slices: tuple[PriceAuditSlice, ...] = ()
    profile: str | None = None
    sources: tuple[str, ...] = ()
    declared_rows: int | None = None
    binding_hashes: tuple[str, ...] = ()


@dataclass(frozen=True)
class PriceAuditPlan:
    identity: str
    units: tuple[PriceAuditUnit, ...]
    files: tuple[tuple[Path, bool], ...]
    sequence: int | None = None
    cutoff: str | None = None
    metadata_integrity: bool = False
    metadata_sources: tuple[PriceAuditSource, ...] = ()


@dataclass(frozen=True)
class PriceAuditSource:
    path: str
    database: SqliteFileStamp
    wal: SqliteFileStamp | None


def _source_stamp(path: Path, *, optional: bool = False) -> SqliteFileStamp | None:
    try:
        value = path.lstat()
    except FileNotFoundError:
        if optional:
            return None
        raise ValueError("price_audit_source_missing_or_unsafe") from None
    if not stat.S_ISREG(value.st_mode):
        raise ValueError("price_audit_source_missing_or_unsafe")
    return SqliteFileStamp(value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def CapturePriceAuditSources(paths: tuple[Path, ...], budget: PriceAuditBudget) -> tuple[PriceAuditSource, ...]:
    """Capture DB/WAL identities before metadata SQL; empty WAL remains distinct from absence."""
    sources: list[PriceAuditSource] = []
    for path in paths:
        budget.check()
        _require_regular(path)
        database = _source_stamp(path)
        if database is None:
            raise ValueError("price_audit_source_missing_or_unsafe")
        sources.append(PriceAuditSource(str(path.resolve()), database, _source_stamp(Path(str(path) + "-wal"), optional=True)))
    return tuple(sources)


def _check_metadata_sources(sources: tuple[PriceAuditSource, ...], budget: PriceAuditBudget) -> None:
    for source in sources:
        budget.check()
        if _source_stamp(Path(source.path), optional=True) != source.database or _source_stamp(Path(source.path + "-wal"), optional=True) != source.wal:
            raise ValueError("price_audit_metadata_source_changed")


@dataclass(frozen=True)
class _FileProof:
    path: str
    size: int
    sha256: str
    modified_ns: int
    receipt_verified: bool


@dataclass
class _FieldCount:
    name: str
    nulls: int = 0
    active_missing: int = 0
    invalid_type: int = 0
    nonfinite: int = 0
    invalid_range: int = 0
    forbidden: int = 0
    active_auxiliary_missing: int = 0


@dataclass
class _StockBits:
    code: str
    expected: int
    observed: int = 0
    trading: int = 0
    confirmed: int = 0
    inferred: int = 0


@dataclass(frozen=True)
class _StockCount:
    code: str
    expected: int
    observed: int
    trading: int
    confirmed: int
    inferred: int
    unexplained: int
    missing_no_row: int
    unvisited: int


@dataclass(frozen=True)
class _UnitResult:
    name: str
    complete: bool
    rows: int
    latest_rows: int
    invalid_flags: int
    invalid_states: int
    raw_ohlc_errors: int
    qfq_ohlc_errors: int
    outside_lifetime: int
    unexpected_dates: int
    logical_errors: int
    integrity: str
    fields: tuple[_FieldCount, ...]
    stocks: tuple[_StockCount, ...]


@dataclass
class _UnitScan:
    unit: PriceAuditUnit
    budget: PriceAuditBudget
    rows: int = 0
    latest_rows: int = 0
    invalid_flags: int = 0
    invalid_states: int = 0
    raw_ohlc_errors: int = 0
    qfq_ohlc_errors: int = 0
    outside_lifetime: int = 0
    unexpected_dates: int = 0
    logical_errors: int = 0
    integrity: str = "not_run"
    fields: tuple[_FieldCount, ...] = field(default_factory=lambda: tuple(_FieldCount(name) for name in _NUMBER_NAMES))
    stocks: tuple[_StockBits, ...] = field(init=False)

    def __post_init__(self) -> None:
        stocks: list[_StockBits] = []
        for security in self.unit.securities:
            self.budget.check()
            expected = sum(
                1 << index for index, day in enumerate(self.unit.dates)
                if day >= security.listed and (security.delisted is None or day < security.delisted)
            )
            stocks.append(_StockBits(security.code, expected))
        self.stocks = tuple(stocks)

    def result(self, *, complete: bool) -> _UnitResult:
        return _UnitResult(
            self.unit.name, complete, self.rows, self.latest_rows, self.invalid_flags, self.invalid_states,
            self.raw_ohlc_errors, self.qfq_ohlc_errors, self.outside_lifetime, self.unexpected_dates, self.logical_errors, self.integrity, self.fields,
            tuple(_StockCount(
                stock.code, stock.expected.bit_count(), (stock.observed & stock.expected).bit_count(),
                (stock.trading & stock.expected).bit_count(), (stock.confirmed & stock.expected).bit_count(),
                (stock.inferred & stock.expected).bit_count(),
                ((stock.expected if complete else stock.expected & stock.observed) & ~(stock.trading | stock.confirmed | stock.inferred)).bit_count(),
                (stock.expected & ~stock.observed).bit_count() if complete else 0,
                0 if complete else (stock.expected & ~stock.observed).bit_count(),
            ) for stock in self.stocks),
        )


def _require_regular(path: Path) -> None:
    if path.is_symlink() or not path.is_file() or any(parent.is_symlink() for parent in path.parents):
        raise ValueError("price_audit_source_missing_or_unsafe")


def _hash_file(path: Path, budget: PriceAuditBudget) -> _FileProof:
    budget.check()
    _require_regular(path)
    before = path.stat()
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while block := source.read(4 * 1024 * 1024):
            budget.check()
            digest.update(block)
    after = path.stat()
    if (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
        after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns,
    ):
        raise ValueError("price_audit_source_changed")
    return _FileProof(str(path.resolve()), before.st_size, digest.hexdigest(), before.st_mtime_ns, False)


def _proofs(plan: PriceAuditPlan, configuration: tuple[Path, ...], budget: PriceAuditBudget, policy: SqliteIoPolicy) -> tuple[_FileProof, ...]:
    proofs: list[_FileProof] = []
    for path, receipt_required in plan.files:
        budget.phase = "sha256:" + path.name
        budget.check()
        if receipt_required:
            verified = VerifySqliteFile(path, sqlite_policy=replace(
                policy, connection_timeout_seconds=min(policy.connection_timeout_seconds, max(0.001, budget.deadline - budget.clock())),
            ), check=budget.check, file_only=True)
            proofs.append(_FileProof(
                str(path.resolve()), verified.database.size, verified.fingerprint.database_sha256,
                verified.database.modified_ns, True,
            ))
            proofs.append(_hash_file(Path(str(path) + ".sha256"), budget))
        else:
            proofs.append(_hash_file(path, budget))
        wal = Path(str(path) + "-wal")
        # An existing empty WAL has a real digest and remains distinct from absence.
        if wal.exists() or wal.is_symlink():
            proofs.append(_hash_file(wal, budget))
        else:
            proofs.append(_FileProof(str(wal.resolve()), 0, "absent", 0, False))
    sources = (
        Path(__file__), Path(__file__).with_name("qfq_sqlite.py"),
        Path(__file__).with_name("cli.py"), Path(__file__).with_name("options.py"), Path(__file__).with_name("commands.py"),
        PROJECT_ROOT / "src/trader/download/infra/price_columns.py",
        PROJECT_ROOT / "src/trader/download/domain/baostock_daily.py",
        PROJECT_ROOT / "src/trader/download/domain/history_control.py",
        PROJECT_ROOT / "src/trader/infra/serialization/canonical_codec.py",
        PROJECT_ROOT / "src/trader/infra/sqlite_publication.py",
        PROJECT_ROOT / "src/trader/infra/sqlite_codec.py",
    )
    for path in (*sources, *configuration):
        budget.phase = "source_configuration_fingerprint"
        proofs.append(_hash_file(path, budget))
    return tuple(proofs)


def LoadPriceAuditReference(
    path: Path,
    publication: str | None,
    budget: PriceAuditBudget,
    policy: SqliteIoPolicy,
    *,
    clear_only: bool = False,
) -> tuple[tuple[PriceAuditSecurity, ...], tuple[int, ...]]:
    """Read published local listing/calendar facts with the same SQL deadline."""
    with budget.connection(path, policy) as connection:
        budget.phase = "reference_sqlite_integrity"
        if tuple(row[0] for row in connection.execute("PRAGMA integrity_check")) != ("ok",):
            raise ValueError("price_audit_reference_integrity_failed")
        if publication is None:
            current = connection.execute("SELECT publication_id FROM reference_current WHERE singleton=1").fetchone()
            if current is None:
                raise ValueError("price_audit_reference_publication_missing")
            publication = str(current[0])
        rows = connection.execute(
            "SELECT s.code,s.listed_date,s.delisted_date FROM reference_member m "
            "JOIN security s ON s.code=m.code AND s.security_id=m.security_id "
            "JOIN security_st_evidence st ON st.code=m.code AND st.st_id=m.st_id "
            "WHERE m.publication_id=?" + (" AND st.st_status='clear'" if clear_only else "") +
            " ORDER BY s.code LIMIT ?", (publication, _MAX_AUDIT_STOCKS + 1),
        ).fetchall()
        if not rows or len(rows) > _MAX_AUDIT_STOCKS:
            raise ValueError("price_audit_reference_universe_invalid")
        if any(not isinstance(row[0], str) or type(row[1]) is not int or row[2] is not None and type(row[2]) is not int for row in rows):
            raise ValueError("price_audit_reference_security_invalid")
        securities = tuple(PriceAuditSecurity(row[0], row[1], row[2]) for row in rows)
        dates = tuple(row[0] for row in connection.execute(
            "SELECT t.trade_date FROM reference_publication p JOIN trading_calendar t ON t.calendar_id=p.calendar_id "
            "WHERE p.publication_id=? ORDER BY t.trade_date LIMIT ?", (publication, _MAX_AUDIT_DATES + 1),
        ))
        if not dates or len(dates) > _MAX_AUDIT_DATES or any(type(day) is not int for day in dates):
            raise ValueError("price_audit_reference_calendar_invalid")
    return securities, dates


def _history_plan(root: Path, budget: PriceAuditBudget, policy: SqliteIoPolicy) -> PriceAuditPlan:
    catalog = root / "catalog.sqlite3"
    metadata_sources = CapturePriceAuditSources((catalog,), budget)
    with budget.connection(catalog, policy) as connection:
        budget.phase = "catalog_sqlite_integrity"
        if tuple(row[0] for row in connection.execute("PRAGMA integrity_check")) != ("ok",):
            raise ValueError("price_audit_catalog_integrity_failed")
        row = connection.execute(
            "SELECT p.sync_sequence,hex(p.snapshot_hash),s.data_cutoff_date,c.publication_id,u.publication_id,sc.reference_file,"
            "c.start_date,c.end_date,c.session_count,hex(c.id),hex(c.source_hash),hex(u.id),hex(u.source_hash) "
            "FROM published_snapshot p JOIN history_snapshot s ON s.id=p.sync_sequence "
            "JOIN calendar_binding c ON c.id=s.calendar_hash JOIN universe_version u ON u.id=s.universe_hash "
            "CROSS JOIN schema_contract sc WHERE p.singleton=1"
        ).fetchone()
        if row is None or row[5] is None:
            raise ValueError("history_publication_or_reference_missing")
        references = connection.execute(
            "SELECT calendar_year,calendar_month,relative_path,sync_sequence,row_count,hex(logical_hash) FROM snapshot_partition "
            "WHERE snapshot_id=? ORDER BY calendar_year,calendar_month", (row[0],),
        ).fetchall()
        snapshot_row = connection.execute(
            "SELECT id,hex(content_hash),data_cutoff_date,label_cutoff_date,hex(calendar_hash),hex(universe_hash),hex(source_hash) "
            "FROM history_snapshot WHERE id=?", (row[0],),
        ).fetchone()
        if snapshot_row is None:
            raise ValueError("price_audit_history_snapshot_missing")
        snapshot = HistorySnapshot(
            int(snapshot_row[0]), DecodeDate(snapshot_row[2]), DecodeDate(snapshot_row[3]),
            str(snapshot_row[4]).lower(), str(snapshot_row[5]).lower(), str(snapshot_row[6]).lower(),
            tuple(HistorySnapshotPartition(str(item[2]), str(item[5]).lower(), int(item[4]), int(item[1]), int(item[3])) for item in references),
        )
        if snapshot.content_hash != str(snapshot_row[1]).lower() or snapshot.content_hash != str(row[1]).lower():
            raise ValueError("price_audit_history_snapshot_identity_mismatch")
    reference_path = (root / str(row[5])).resolve()
    price_paths = tuple(root / str(relative) for relative in sorted({item[2] for item in references}))
    metadata_sources += CapturePriceAuditSources((reference_path, *price_paths), budget)
    securities, _ = LoadPriceAuditReference(reference_path, str(row[4]), budget, policy, clear_only=True)
    _, bound_calendar = LoadPriceAuditReference(reference_path, str(row[3]), budget, policy)
    dates = tuple(day for day in bound_calendar if int(row[6]) <= day <= int(row[7]))
    if len(dates) != int(row[8]):
        raise ValueError("history_calendar_count_mismatch")
    if HistoryCalendarIdentity(tuple(DecodeDate(day) for day in dates), str(row[10]).lower()).content_hash != str(row[9]).lower():
        raise ValueError("history_calendar_identity_mismatch")
    budget.phase = "calendar_partition_coverage"
    expected_months = frozenset((DecodeDate(day).year, DecodeDate(day).month) for day in dates)
    declared_months = frozenset((int(item[0]), int(item[1])) for item in references)
    if expected_months != declared_months:
        raise ValueError("price_audit_history_partition_calendar_mismatch")
    # Security names/boards participate in the official universe digest.
    budget.phase = "universe_identity_validation"
    with budget.connection(reference_path, policy) as connection:
        typed = tuple(HistorySecurityIdentity(
            str(item[0]), str(item[1]), cast(HistorySecurityBoard, item[2]), DecodeDate(item[3]), DecodeDate(item[4]) if item[4] is not None else None,
        ) for item in connection.execute(
            "SELECT s.code,s.name,s.board,s.listed_date,s.delisted_date FROM reference_member m "
            "JOIN security s ON s.code=m.code AND s.security_id=m.security_id "
            "JOIN security_st_evidence st ON st.code=m.code AND st.st_id=m.st_id "
            "WHERE m.publication_id=? AND st.st_status='clear' ORDER BY s.code", (row[4],),
        ))
    if HistoryUniverseIdentity(typed, str(row[12]).lower()).content_hash != str(row[11]).lower():
        raise ValueError("history_universe_identity_mismatch")
    units: list[PriceAuditUnit] = []
    files: list[tuple[Path, bool]] = [(catalog, True), (reference_path, False)]
    for relative, group in groupby(sorted(references, key=lambda item: item[2]), key=lambda item: item[2]):
        monthly = tuple(group)
        path = root / str(relative)
        if path.resolve().parent != root.resolve() or path.is_symlink():
            raise ValueError("history_partition_path_unsafe")
        unit_dates = tuple(day for day in dates if any(
            DecodeDate(day).year == month[0] and DecodeDate(day).month == month[1] for month in monthly
        ))
        slices = tuple(PriceAuditSlice(
            max(unit_dates[0], (date(int(month[0]), int(month[1]), 1) - date(1970, 1, 1)).days),
            min(unit_dates[-1], (date(int(month[0]), int(month[1]), monthrange(int(month[0]), int(month[1]))[1]) - date(1970, 1, 1)).days), int(month[3]),
        ) for month in monthly)
        units.append(PriceAuditUnit(str(relative), path, unit_dates, securities, slices, declared_rows=sum(int(month[4]) for month in monthly)))
        files.append((path, True))
    if not units or len(units) > 32:
        raise ValueError("history_price_units_invalid")
    return PriceAuditPlan(str(row[1]).lower(), tuple(units), tuple(files), int(row[0]), DecodeDate(row[2]).isoformat(), True, metadata_sources)


def _scan_row(scan: _UnitScan, stock: _StockBits, bit: int, values: tuple[object, ...]) -> None:
    scan.rows += 1
    if bit:
        stock.observed |= bit
        if not stock.expected & bit:
            scan.outside_lifetime += 1
    elif scan.unit.profile is not None:
        scan.unexpected_dates += 1
    bad = False
    try:
        DecodePriceValues(cast(PriceValues, values))
    except (ValueError, TypeError, OverflowError):
        scan.invalid_flags += 1
        bad = True
    if len(values) != len(PRICE_COLUMNS):
        scan.invalid_states += 1
        return
    status = values[0]
    states = ((values[1], values[11]), (values[12], values[22]))
    if status == "complete":
        state_valid = states == ((1, "trading"), (1, "trading"))
    elif status == "supplier_marked_suspended":
        state_valid = states == ((1, "suspended"), (1, "suspended"))
    elif status in ("tencent_inferred_suspended", "unknown_missing"):
        state_valid = states == ((0, None), (0, None))
    elif status == "qfq_missing":
        state_valid = states[0][0] == 1 and states[0][1] in ("trading", "suspended") and states[1] == (0, None)
    elif status == "unadjusted_missing":
        state_valid = states[0] == (0, None) and states[1][0] == 1 and states[1][1] in ("trading", "suspended")
    else:
        state_valid = False
    if not state_valid:
        scan.invalid_states += 1
        bad = True
    for index, (position, metric) in enumerate(zip(_NUMBER_POSITIONS, scan.fields, strict=True)):
        value = values[position]
        local = index % 9
        side = index // 9
        active = states[side] == (1, "trading")
        if value is None:
            metric.nulls += 1
            if active and local < 6:
                metric.active_missing += 1
                bad = True
            elif active and side == 0:
                metric.active_auxiliary_missing += 1
            continue
        if type(value) not in (float, int):
            metric.invalid_type += 1
            bad = True
            continue
        number = cast(float | int, value)
        if not math.isfinite(number):
            metric.nonfinite += 1
            bad = True
        elif number < 0 and local != 7 or active and local < 4 and number == 0:
            metric.invalid_range += 1
            bad = True
        if side == 1 and local >= 6:
            metric.forbidden += 1
            bad = True
        if states[side][0] == 0:
            scan.invalid_states += 1
            bad = True
    for side, start in enumerate((2, 13)):
        prices = values[start:start + 4]
        if all(type(value) in (float, int) and math.isfinite(cast(float, value)) for value in prices):
            opening, high, low, closing = cast(tuple[float, float, float, float], prices)
            if not low <= min(opening, closing) <= max(opening, closing) <= high:
                if side == 0:
                    scan.raw_ohlc_errors += 1
                else:
                    scan.qfq_ohlc_errors += 1
                bad = True
    if not bad:
        if status == "complete":
            stock.trading |= bit
        elif status == "supplier_marked_suspended":
            stock.confirmed |= bit
        elif status == "tencent_inferred_suspended":
            stock.inferred |= bit


def _scan_unit(scan: _UnitScan, budget: PriceAuditBudget, policy: SqliteIoPolicy) -> None:
    unit = scan.unit
    stocks = {stock.code: stock for stock in scan.stocks}
    days = {day: 1 << index for index, day in enumerate(unit.dates)}
    budget.phase = unit.name + ":sqlite_integrity"
    with budget.connection(unit.path, policy) as connection:
        integrity = tuple(row[0] for row in connection.execute("PRAGMA integrity_check"))
        scan.integrity = "passed" if integrity == ("ok",) else "failed"
        if scan.integrity != "passed":
            raise ValueError("price_audit_integrity_failed")
        if unit.profile is not None:
            budget.phase = unit.name + ":fields_and_coverage"
            cursor = connection.execute(
                "SELECT trade_date,code," + ",".join(PRICE_COLUMNS) +
                " FROM daily_price WHERE trade_date BETWEEN ? AND ? ORDER BY code,trade_date",
                (unit.dates[0], unit.dates[-1]),
            )
            digests = {stock.code: hashlib.sha256(b"[") for stock in unit.securities}
            hashed: set[str] = set()
            while batch := cursor.fetchmany(512):
                budget.check()
                for row in batch:
                    if row[1] in stocks:
                        _scan_row(scan, stocks[row[1]], days.get(row[0], 0), tuple(row[2:]))
                        try:
                            encoded = json.dumps((DecodeDate(row[0]).isoformat(), DecodePriceValues(tuple(row[2:]))), separators=(",", ":"), allow_nan=False).encode("ascii")
                        except (ValueError, TypeError, OverflowError):
                            scan.logical_errors += 1
                            continue
                        if row[1] in hashed:
                            digests[row[1]].update(b",")
                        digests[row[1]].update(encoded)
                        hashed.add(row[1])
            for security, expected in zip(unit.securities, unit.binding_hashes, strict=True):
                digests[security.code].update(b"]")
                if digests[security.code].hexdigest() != expected:
                    scan.logical_errors += 1
            budget.phase = unit.name + ":nontrading_evidence"
            today = (datetime.now(ZoneInfo("Asia/Shanghai")).date() - date(1970, 1, 1)).days
            sources = dict(zip((stock.code for stock in unit.securities), unit.sources, strict=True))
            for code, day, kind, checked, source in connection.execute(
                "SELECT code,trade_date,kind,checked_on,source FROM price_nontrading ORDER BY code,trade_date"
            ):
                budget.check()
                if code not in stocks or day not in days:
                    continue
                stock, bit = stocks[code], days[day]
                if kind == "legacy_unverified":
                    continue
                if kind not in ("confirmed", "inferred") or checked is None or not day <= checked <= today or source != sources[code]:
                    scan.invalid_states += 1
                    continue
                if stock.trading & bit:
                    scan.invalid_states += 1
                elif kind == "confirmed":
                    stock.confirmed |= bit
                else:
                    stock.inferred |= bit
            return
        # Materialize latest observation identities once. No source-file lookup
        # is performed per revision: only the bounded in-memory index is probed.
        budget.phase = unit.name + ":latest_observation_index"
        sequence = "CASE " + " ".join("WHEN trade_date BETWEEN ? AND ? THEN ?" for _ in unit.slices) + " ELSE 0 END"
        parameters = (unit.dates[0], unit.dates[-1], *(value for part in unit.slices for value in (part.first, part.last, part.sequence)))
        with closing(sqlite3.connect(":memory:")) as latest:
            latest.execute("CREATE TABLE selected(code TEXT,day INTEGER,revision TEXT,PRIMARY KEY(code,day)) WITHOUT ROWID")
            cursor = connection.execute(
                "SELECT trade_date,code,sync_sequence,revision_id FROM daily_price_observation "
                "WHERE trade_date BETWEEN ? AND ? AND sync_sequence<=" + sequence +
                " ORDER BY trade_date,code,sync_sequence", parameters,
            )
            previous: tuple[int, str, str] | None = None
            selected: list[tuple[str, int, str]] = []
            while batch := cursor.fetchmany(1024):
                budget.check()
                for row in batch:
                    if row[1] not in stocks:
                        continue
                    current = (int(row[0]), str(row[1]), str(row[3]))
                    if previous is not None and previous[:2] != current[:2]:
                        selected.append((previous[1], previous[0], previous[2]))
                        scan.latest_rows += 1
                        if previous[0] not in days:
                            scan.unexpected_dates += 1
                    previous = current
                if scan.latest_rows > _MAX_LATEST_ROWS:
                    raise ValueError("price_audit_latest_index_limit")
                latest.executemany("INSERT INTO selected VALUES(?,?,?)", selected)
                selected.clear()
            if previous is not None:
                scan.latest_rows += 1
                if previous[0] not in days:
                    scan.unexpected_dates += 1
                if scan.latest_rows > _MAX_LATEST_ROWS:
                    raise ValueError("price_audit_latest_index_limit")
                latest.execute("INSERT INTO selected VALUES(?,?,?)", (previous[1], previous[0], previous[2]))
            budget.phase = unit.name + ":fields_and_coverage"
            cursor = connection.execute(
                "SELECT trade_date,code,revision_id," + ",".join(PRICE_COLUMNS) +
                " FROM daily_price_revision WHERE trade_date BETWEEN ? AND ? ORDER BY trade_date,code,revision_id",
                (unit.dates[0], unit.dates[-1]),
            )
            probe = latest.cursor()
            while batch := cursor.fetchmany(512):
                budget.check()
                for row in batch:
                    if row[1] not in stocks:
                        continue
                    selected = probe.execute("SELECT revision FROM selected WHERE code=? AND day=?", (row[1], row[0])).fetchone()
                    if selected is not None and selected[0] == row[2]:
                        _scan_row(scan, stocks[row[1]], days.get(row[0], 0), tuple(row[3:]))


def _unit_document(result: _UnitResult) -> dict[str, object]:
    return {
        "name": result.name, "complete": result.complete, "actual_selected_rows": result.rows,
        "latest_observation_rows": result.latest_rows, "invalid_number_flags": result.invalid_flags,
        "invalid_cell_states": result.invalid_states, "raw_ohlc_errors": result.raw_ohlc_errors,
        "qfq_ohlc_errors": result.qfq_ohlc_errors, "outside_listing_interval": result.outside_lifetime,
        "unexpected_calendar_stock_days": result.unexpected_dates,
        "binding_hash_mismatches": result.logical_errors,
        "sqlite_integrity": result.integrity,
        "fields": [{
            "column": metric.name, "nulls": metric.nulls, "active_core_nulls": metric.active_missing,
            "invalid_type": metric.invalid_type, "nonfinite": metric.nonfinite,
            "invalid_range": metric.invalid_range, "forbidden_qfq_raw_only_values": metric.forbidden,
            "active_raw_auxiliary_nulls": metric.active_auxiliary_missing,
        } for metric in result.fields],
        "stock_date_coverage": [{
            "code": stock.code, "expected_stock_days": stock.expected, "actual_row_days": stock.observed,
            "trading_days": stock.trading, "confirmed_nontrading_days": stock.confirmed,
            "inferred_nontrading_days": stock.inferred, "unexplained_days": stock.unexplained,
            "missing_price_row_days": stock.missing_no_row,
            "unvisited_stock_days": stock.unvisited,
        } for stock in result.stocks],
    }


def _checkpoint_path(path: Path | None, sources: tuple[tuple[Path, bool], ...]) -> Path | None:
    if path is None:
        return None
    if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
        raise ValueError("price_audit_checkpoint_unsafe")
    target = ExternalPath(path, "--audit-checkpoint")
    assert target is not None
    if not target.parent.is_dir() or target.exists() and not target.is_file():
        raise ValueError("price_audit_checkpoint_unsafe")
    for source, _ in sources:
        if target == source.resolve() or target == Path(str(source.resolve()) + "-wal") or target == Path(str(source.resolve()) + ".sha256"):
            raise ValueError("price_audit_checkpoint_input_conflict")
        if target.is_relative_to(source.resolve().parent):
            raise ValueError("price_audit_checkpoint_must_be_outside_source_directory")
    return target


def _save_checkpoint(path: Path | None, fingerprint: str, units: tuple[_UnitResult, ...], budget: PriceAuditBudget) -> None:
    budget.check()
    if path is None:
        return
    budget.phase = "checkpoint:encoding"
    documents: list[dict[str, object]] = []
    for unit in units:
        budget.check()
        documents.append(_unit_document(unit))
    document = {"fingerprint": fingerprint, "completed_units": documents, "results_sha256": CanonicalDigest(documents)}
    budget.check()
    payload = json.dumps(document, separators=(",", ":"), allow_nan=False).encode()
    budget.check()
    if len(payload) > _MAX_CHECKPOINT_BYTES:
        raise ValueError("price_audit_checkpoint_size_limit")
    temporary: Path | None = None
    try:
        budget.phase = "checkpoint:write"
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=path.name + ".", delete=False) as output:
            temporary = Path(output.name)
            for offset in range(0, len(payload), 1024 * 1024):
                budget.check()
                output.write(payload[offset:offset + 1024 * 1024])
            budget.check()
            output.flush()
            budget.check()
            budget.phase = "checkpoint:fsync"
            os.fsync(output.fileno())
            budget.check()
        if path.is_symlink():
            raise ValueError("price_audit_checkpoint_unsafe")
        budget.phase = "checkpoint:commit"
        budget.check()
        os.replace(temporary, path)
        budget.check()
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _checkpoint_integer(record: dict[str, object], key: str) -> int:
    value = record.get(key)
    if type(value) is not int or not 0 <= value <= 100_000_000:
        raise ValueError("price_audit_checkpoint_invalid")
    return value


def _checkpoint_record(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError("price_audit_checkpoint_invalid")
    return cast(dict[str, object], value)


def _load_checkpoint(path: Path | None, fingerprint: str, plan: PriceAuditPlan, budget: PriceAuditBudget) -> tuple[_UnitResult, ...]:
    budget.phase = "checkpoint:validation"
    budget.check()
    if path is None or not path.exists():
        return ()
    _require_regular(path)
    if path.stat().st_size > _MAX_CHECKPOINT_BYTES:
        raise ValueError("price_audit_checkpoint_size_limit")
    with path.open("rb") as source:
        payload = source.read(_MAX_CHECKPOINT_BYTES + 1)
    if len(payload) > _MAX_CHECKPOINT_BYTES:
        raise ValueError("price_audit_checkpoint_size_limit")
    budget.check()
    document = _checkpoint_record(json.loads(payload))
    budget.check()
    if document.get("fingerprint") != fingerprint:
        raise ValueError("price_audit_checkpoint_fingerprint_mismatch")
    entries = document.get("completed_units")
    if not isinstance(entries, list) or len(entries) > len(plan.units):
        raise ValueError("price_audit_checkpoint_invalid")
    if document.get("results_sha256") != CanonicalDigest(entries):
        raise ValueError("price_audit_checkpoint_results_mismatch")
    budget.check()
    results: list[_UnitResult] = []
    for index, entry in enumerate(entries):
        budget.check()
        record = _checkpoint_record(entry)
        if record.get("name") != plan.units[index].name or record.get("complete") is not True or record.get("sqlite_integrity") != "passed":
            raise ValueError("price_audit_checkpoint_invalid")
        field_entries = record.get("fields")
        stock_entries = record.get("stock_date_coverage")
        if not isinstance(field_entries, list) or len(field_entries) != len(_NUMBER_NAMES):
            raise ValueError("price_audit_checkpoint_invalid")
        if not isinstance(stock_entries, list) or len(stock_entries) != len(plan.units[index].securities):
            raise ValueError("price_audit_checkpoint_invalid")
        fields: list[_FieldCount] = []
        for name, item in zip(_NUMBER_NAMES, field_entries, strict=True):
            budget.check()
            metric = _checkpoint_record(item)
            if metric.get("column") != name:
                raise ValueError("price_audit_checkpoint_invalid")
            fields.append(_FieldCount(name, *(_checkpoint_integer(metric, key) for key in (
                "nulls", "active_core_nulls", "invalid_type", "nonfinite", "invalid_range", "forbidden_qfq_raw_only_values", "active_raw_auxiliary_nulls",
            ))))
        stocks: list[_StockCount] = []
        for security, item in zip(plan.units[index].securities, stock_entries, strict=True):
            budget.check()
            stock = _checkpoint_record(item)
            if stock.get("code") != security.code:
                raise ValueError("price_audit_checkpoint_invalid")
            stocks.append(_StockCount(security.code, *(_checkpoint_integer(stock, key) for key in (
                "expected_stock_days", "actual_row_days", "trading_days", "confirmed_nontrading_days",
                "inferred_nontrading_days", "unexplained_days", "missing_price_row_days",
                "unvisited_stock_days",
            ))))
        expected_stocks = _UnitScan(plan.units[index], budget).stocks
        if any(
            stock.expected != expected.expected.bit_count() or stock.observed > stock.expected
            or stock.missing_no_row != stock.expected - stock.observed or stock.unvisited != 0
            or stock.trading + stock.confirmed + stock.inferred + stock.unexplained != stock.expected
            for stock, expected in zip(stocks, expected_stocks, strict=True)
        ):
            raise ValueError("price_audit_checkpoint_invalid")
        results.append(_UnitResult(
            name=plan.units[index].name,
            complete=True,
            rows=_checkpoint_integer(record, "actual_selected_rows"),
            latest_rows=_checkpoint_integer(record, "latest_observation_rows"),
            invalid_flags=_checkpoint_integer(record, "invalid_number_flags"),
            invalid_states=_checkpoint_integer(record, "invalid_cell_states"),
            raw_ohlc_errors=_checkpoint_integer(record, "raw_ohlc_errors"),
            qfq_ohlc_errors=_checkpoint_integer(record, "qfq_ohlc_errors"),
            outside_lifetime=_checkpoint_integer(record, "outside_listing_interval"),
            unexpected_dates=_checkpoint_integer(record, "unexpected_calendar_stock_days"),
            logical_errors=_checkpoint_integer(record, "binding_hash_mismatches"),
            integrity="passed",
            fields=tuple(fields),
            stocks=tuple(stocks),
        ))
    return tuple(results)


def AuditPriceFields(
    plan: PriceAuditPlan,
    *,
    budget: PriceAuditBudget,
    sqlite_policy: SqliteIoPolicy,
    checkpoint: Path | None = None,
    configuration: tuple[Path, ...] = (),
) -> dict[str, object]:
    """Scan complete published units; interrupted units never become reusable checkpoints."""
    try:
        budget.check()
    except PriceAuditInterrupted as exc:
        return {
            "complete": False, "status": "failed", "failure": "price_audit_" + str(exc),
            "phase": budget.phase, "units": [], "actual_selected_rows": 0,
            "completed_units": 0, "total_units": len(plan.units), "unvisited_units": len(plan.units),
            "source_data_writes": 0, "network_requests": 0,
        }
    if not plan.units or len(plan.units) > 32 or len({unit.name for unit in plan.units}) != len(plan.units):
        raise ValueError("price_audit_units_invalid")
    for unit in plan.units:
        if (
            not unit.dates or len(unit.dates) > _MAX_AUDIT_DATES or unit.dates != tuple(sorted(set(unit.dates)))
            or not unit.securities or len(unit.securities) > _MAX_AUDIT_STOCKS
            or tuple(item.code for item in unit.securities) != tuple(sorted({item.code for item in unit.securities}))
            or unit.path not in tuple(path for path, _ in plan.files)
            or unit.profile is not None and (len(unit.sources) != len(unit.securities) or len(unit.binding_hashes) != len(unit.securities))
            or unit.profile is None and not unit.slices
        ):
            raise ValueError("price_audit_unit_invalid")
    path = _checkpoint_path(checkpoint, plan.files + tuple((item, False) for item in configuration))
    completed: tuple[_UnitResult, ...] = ()
    partial: _UnitResult | None = None
    proofs: tuple[_FileProof, ...] = ()
    fingerprint: str | None = None
    resumed = 0
    failure: str | None = None
    try:
        budget.phase = "metadata_source_stability"
        _check_metadata_sources(plan.metadata_sources, budget)
        proofs = _proofs(plan, configuration, budget, sqlite_policy)
        budget.phase = "metadata_source_stability"
        _check_metadata_sources(plan.metadata_sources, budget)
        scope = tuple((unit.name, str(unit.path.resolve()), unit.dates, unit.securities, unit.slices,
                       unit.profile, unit.sources, unit.binding_hashes, unit.declared_rows) for unit in plan.units)
        fingerprint = CanonicalDigest((plan.identity, plan.sequence, scope, proofs, sqlite_policy))
        completed = _load_checkpoint(path, fingerprint, plan, budget)
        resumed = len(completed)
        for unit in plan.units[resumed:]:
            budget.check()
            budget.phase = unit.name + ":expected_date_inventory"
            scan = _UnitScan(unit, budget)
            try:
                _scan_unit(scan, budget, sqlite_policy)
            except (PriceAuditInterrupted, ValueError, sqlite3.Error):
                partial = scan.result(complete=False)
                raise
            completed += (scan.result(complete=True),)
            _save_checkpoint(path, fingerprint, completed, budget)
        budget.phase = "source_stability"
        _check_metadata_sources(plan.metadata_sources, budget)
        for proof in proofs:
            budget.check()
            source = Path(proof.path)
            if proof.sha256 == "absent":
                if source.exists():
                    raise ValueError("price_audit_source_changed")
            elif not source.is_file() or (source.stat().st_size, source.stat().st_mtime_ns) != (proof.size, proof.modified_ns):
                raise ValueError("price_audit_source_changed")
    except PriceAuditInterrupted as exc:
        failure = "price_audit_" + str(exc)
    except (OSError, ValueError, sqlite3.Error, SqlitePublicationError) as exc:
        if isinstance(exc, SqlitePublicationError):
            failure = "price_audit_publication_verification_failed"
        else:
            failure = str(exc) if isinstance(exc, ValueError) and str(exc).startswith("price_audit_") else "price_audit_operation_failed"
    units = completed + ((partial,) if partial is not None else ())
    complete = failure is None and len(completed) == len(plan.units)
    invalid = sum(
        unit.invalid_flags + unit.invalid_states + unit.raw_ohlc_errors + unit.qfq_ohlc_errors + unit.outside_lifetime + unit.unexpected_dates + unit.logical_errors +
        sum(metric.active_missing + metric.invalid_type + metric.nonfinite + metric.invalid_range + metric.forbidden for metric in unit.fields)
        for unit in units
    )
    inferred = sum(stock.inferred for unit in units for stock in unit.stocks)
    unexplained = sum(stock.unexplained for unit in units for stock in unit.stocks)
    orphaned = sum(max(0, unit.latest_rows - unit.rows) for unit in units if unit.complete)
    auxiliary_missing = sum(metric.active_auxiliary_missing for unit in units for metric in unit.fields)
    return {
        "complete": complete, "status": "failed" if failure or invalid or orphaned else "degraded" if inferred or unexplained or auxiliary_missing else "passed",
        "failure": failure, "phase": budget.phase, "source_data_writes": 0, "network_requests": 0,
        "publication_identity": plan.identity, "sequence": plan.sequence, "data_cutoff": plan.cutoff,
        "completed_units": len(completed), "total_units": len(plan.units), "resumed_units": resumed,
        "unvisited_units": len(plan.units) - len(units),
        "unvisited_stock_days_in_partial_unit": sum(stock.unvisited for unit in units for stock in unit.stocks),
        "actual_selected_rows": sum(unit.rows for unit in units), "invalid_values": invalid,
        "unexpected_calendar_stock_days": sum(unit.unexpected_dates for unit in units),
        "missing_selected_revision_rows": orphaned, "confirmed_nontrading_days": sum(stock.confirmed for unit in units for stock in unit.stocks),
        "inferred_nontrading_days": inferred, "unexplained_days": unexplained,
        "active_raw_auxiliary_null_values": auxiliary_missing,
        "all_active_raw_auxiliary_fields_present": complete and auxiliary_missing == 0,
        "metadata_sqlite_integrity": "passed" if plan.metadata_integrity else "not_run",
        "sha256_validation": "passed" if proofs else "incomplete", "fingerprint": fingerprint,
        "field_validation": "failed" if invalid else "passed" if complete else "incomplete",
        "sqlite_integrity_validation": "failed" if any(unit.integrity == "failed" for unit in units) else "passed" if len(units) == len(plan.units) and all(unit.integrity == "passed" for unit in units) else "incomplete",
        "binding_content_validation": "not_applicable" if all(unit.profile is None for unit in plan.units) else "failed" if any(unit.logical_errors for unit in units) else "passed" if complete else "incomplete",
        "files": [{"path": proof.path, "bytes": proof.size, "sha256": proof.sha256, "publication_receipt_verified": proof.receipt_verified} for proof in proofs],
        "units": [{**_unit_document(unit), "declared_manifest_rows": plan.units[index].declared_rows} for index, unit in enumerate(units)],
        "interpretation": {
            "qfq_preclose_change_turnover_null": "legal_optional_unadjusted_only_fields",
            "raw_auxiliary_null": "original_supplier_fields_not_preserved_not_zero_and_not_derived",
            "confirmed": "published_supplier_state_or_matching_dated_local_evidence_not_original_quote_reverification",
            "inferred": "not_confirmed_suspension_and_not_excluded_from_strict_completeness",
            "unexplained": "missing_or_invalid_price_without_confirmed_or_inferred_fact",
            "revision_hashes": "file_receipts_verified_individual_revision_hashes_not_recomputed",
            "partial_coverage": "unvisited_dates_remain_unknown_until_complete",
            "violation_counts": "counts_of_conditions_not_distinct_bad_rows",
            "profile_counts": "overlapping_qfq_profiles_are_counted_separately_not_unique_physical_rows",
        },
    }


def AuditHistoryPriceFields(
    root: Path,
    *,
    sqlite_policy: SqliteIoPolicy,
    budget: PriceAuditBudget,
    checkpoint: Path | None = None,
    configuration: tuple[Path, ...] = (),
) -> dict[str, object]:
    """Explicit full field scan using the formal snapshot and each month's sequence."""
    plan = _history_plan(root, budget, sqlite_policy)
    fields = AuditPriceFields(plan, budget=budget, sqlite_policy=sqlite_policy, checkpoint=checkpoint, configuration=configuration)
    return {"schema_version": "history-publication-audit", "status": fields["status"], "summary": {
        "read_only": True, "source_data_writes": 0, "network_requests": 0,
        "snapshot_hash": plan.identity, "sequence": plan.sequence, "data_cutoff": plan.cutoff,
        "verified_rows": fields["actual_selected_rows"], "field_audit": fields,
        "scope": "published_prices_local_reference_and_source_receipts_not_supplier_revalidation",
    }}


def _receipts(path: Path, *, sqlite_policy: SqliteIoPolicy) -> dict[str, object]:
    with (
        SqliteSession(path, SqliteConnectionOptions(read_only=True), sqlite_policy=sqlite_policy) as connection,
        SqliteTransaction(connection),
    ):
        batch = connection.run(
            "SELECT sync_sequence,cutoff_date,source_hash,calendar_hash,universe_hash FROM download_batch"
        ).fetchone()
        gaps = connection.run("SELECT COUNT(*),COUNT(DISTINCT code) FROM download_gaps").require_one()
        tasks = connection.run(
            "SELECT COUNT(*),COALESCE(SUM(tencent_done=0),0) FROM download_tasks"
        ).require_one()
        task_members = CanonicalDigest(tuple(row[0] for row in connection.run("SELECT code FROM download_tasks ORDER BY code")))
        candidates = connection.run("SELECT COUNT(*) FROM price_replacement").require_one()[0]
        unresolved = connection.run(
            "SELECT COUNT(*) FROM price_repair_attempt WHERE state<>'resolved'"
        ).require_one()[0]
        attempts = [
            {"state": row[0], "days": row[1], "stocks": row[2]}
            for row in connection.run(
                "SELECT state,COUNT(*),COUNT(DISTINCT code) FROM price_repair_attempt GROUP BY state ORDER BY state"
            )
        ]
    return {
        "batch_sequence": batch[0] if batch else None,
        "batch_cutoff": DecodeDate(batch[1]).isoformat() if batch else None,
        "batch_source_hash": DecodeHash(batch[2]) if batch else None,
        "batch_calendar_hash": DecodeHash(batch[3]) if batch else None,
        "batch_universe_hash": DecodeHash(batch[4]) if batch else None,
        "gap_days": gaps[0],
        "gap_stocks": gaps[1],
        "tasks": tasks[0],
        "task_members_hash": task_members,
        "unfinished_tasks": tasks[1],
        "replacement_rows": candidates,
        "unresolved_repair_days": unresolved,
        "repair_attempts": attempts,
    }


def AuditHistoryPublication(root: Path, *, sqlite_policy: SqliteIoPolicy) -> dict[str, object]:
    """Verify every published month using the production codec, without repair or initialization."""
    path = root / "catalog.sqlite3"
    if path.is_symlink() or not path.is_file():
        raise FileNotFoundError("history_catalog_unavailable")
    control = HistoryControlIndex(path, sqlite_policy=sqlite_policy)
    if control.integrity().state != "healthy":
        raise RuntimeError("history_catalog_invalid")
    before = control.load_published_state()
    if before is None:
        raise RuntimeError("history_publication_missing")
    receipts = _receipts(path, sqlite_policy=sqlite_policy)
    snapshot = before.snapshot
    checked_paths: set[Path] = set()
    rows = 0
    published_gaps = 0
    codes = frozenset(security.code for security in before.universe.securities)
    dates = frozenset(before.calendar.open_dates)
    for relative_path, group in groupby(snapshot.partitions, key=lambda item: item.relative_path):
        partitions = tuple(group)
        price_path = root / relative_path
        if price_path.is_symlink() or not price_path.is_file():
            raise FileNotFoundError("history_partition_unavailable")
        if price_path not in checked_paths:
            with SqliteSession(
                price_path, SqliteConnectionOptions(read_only=True), sqlite_policy=sqlite_policy
            ) as connection:
                CheckSqliteIntegrity(connection)
            checked_paths.add(price_path)
        HistoryPricePartition.verify_months(price_path, partitions, sqlite_policy=sqlite_policy)
        for partition in partitions:
            monthly = HistoryPricePartition(
                price_path, partition.calendar_year, partition.calendar_month, sqlite_policy=sqlite_policy
            )
            published_gaps += sum(
                code in codes and day in dates for code, day in monthly.missing_price_dates(partition.sequence)
            )
            rows += partition.row_count
    failures: list[str] = []
    if control.load_published_state() != before or _receipts(path, sqlite_policy=sqlite_policy) != receipts:
        failures.append("history_changed_during_audit")
    pending: list[str] = []
    if receipts["gap_days"]:
        pending.append("history_price_gaps")
    if published_gaps:
        pending.append("history_published_price_gaps")
    if receipts["unfinished_tasks"]:
        pending.append("history_tasks_unfinished")
    if receipts["replacement_rows"]:
        pending.append("history_replacement_pending")
    if receipts["unresolved_repair_days"]:
        pending.append("history_repair_unresolved")
    if (
        receipts["batch_sequence"] != snapshot.sequence
        or receipts["batch_cutoff"] != snapshot.data_cutoff.isoformat()
        or receipts["batch_source_hash"] != snapshot.source_identity_hash
        or receipts["batch_calendar_hash"] != snapshot.calendar_hash
        or receipts["batch_universe_hash"] != snapshot.universe_hash
    ):
        pending.append("history_batch_publication_mismatch")
    if receipts["task_members_hash"] != CanonicalDigest(tuple(sorted(codes))):
        pending.append("history_task_membership_mismatch")
    return {
        "schema_version": "history-publication-audit",
        "status": "failed" if failures else "degraded" if pending else "passed",
        "summary": {
            "read_only": True,
            "source_data_writes": 0,
            "network_requests": 0,
            "snapshot_hash": snapshot.content_hash,
            "sequence": snapshot.sequence,
            "data_cutoff": snapshot.data_cutoff.isoformat(),
            "sessions": len(before.calendar.open_dates),
            "stocks": len(before.universe.securities),
            "annual_databases": len(checked_paths),
            "verified_months": len(snapshot.partitions),
            "verified_rows": rows,
            "published_gap_days": published_gaps,
            "receipts": receipts,
            "failures": failures,
            "pending": pending,
            "scope": "published_content_and_receipts_not_supplier_completeness",
        },
    }


def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history-root", type=Path, required=True)
    parser.add_argument("--runtime-config", type=Path, required=True)
    parser.add_argument("--price-field-audit", action="store_true")
    parser.add_argument("--audit-checkpoint", type=Path)
    parser.add_argument("--audit-budget-seconds", type=float, default=170.0)
    args = parser.parse_args()
    from trader.bootstrap import BuildSqlitePolicy
    from trader.infra.settings import LoadRuntimeSettings

    stopped = False
    def cancel(_signal: int, _frame: object) -> None:
        nonlocal stopped
        stopped = True
    previous_signal = signal.signal(signal.SIGINT, cancel) if args.price_field_audit else None
    budget = PriceAuditBudget(args.audit_budget_seconds, cancelled=lambda: stopped)
    try:
        budget.check()
        policy = BuildSqlitePolicy(LoadRuntimeSettings(args.runtime_config))
        budget.check()
        if args.price_field_audit:
            report = AuditHistoryPriceFields(
                args.history_root, sqlite_policy=policy, budget=budget, checkpoint=args.audit_checkpoint,
                configuration=(args.runtime_config, args.runtime_config.parent / "download.json"),
            )
        else:
            report = AuditHistoryPublication(args.history_root, sqlite_policy=policy)
    except PriceAuditInterrupted as exc:
        report = {"schema_version": "history-publication-audit", "status": "failed", "summary": {
            "field_audit": {"complete": False, "failure": "price_audit_" + str(exc), "phase": budget.phase},
        }}
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
        summary: dict[str, object] = {
            "failure": "history_publication_audit_failed", "category": type(exc).__name__,
            "diagnostic": HistoryFailureDetail(exc),
        }
        if args.price_field_audit:
            summary["field_audit"] = {
                "complete": False, "phase": budget.phase,
                "failure": str(exc) if isinstance(exc, ValueError) and str(exc).startswith("price_audit_") else "price_audit_metadata_failed",
                "category": type(exc).__name__, "units": [], "actual_selected_rows": 0,
            }
        report = {
            "schema_version": "history-publication-audit",
            "status": "failed",
            "summary": summary,
        }
    finally:
        if previous_signal is not None:
            signal.signal(signal.SIGINT, previous_signal)
    emit_report(report)
    return 1 if report["status"] == "failed" else 0


def Main() -> int:
    raise SystemExit(_main())
    return 0
