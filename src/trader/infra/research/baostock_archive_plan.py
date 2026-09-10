"""Build a read-only parent-archive reuse and incremental acquisition plan."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import cast

from trader.infra.scoring.profiles.v3.bundle_store import locate_active_tomorrow_bundle


@dataclass(frozen=True)
class ArchiveRequestEstimate:
    daily_raw: int
    daily_qfq: int
    is_st: int
    industry: int
    context: int

    @property
    def total(self) -> int:
        return self.daily_raw + self.daily_qfq + self.is_st + self.industry + self.context


@dataclass(frozen=True)
class FieldFamilyCoverage:
    reusable_rows: int
    missing_rows: int
    missing_reason: str | None = None


@dataclass(frozen=True)
class ArchiveFieldCoverage:
    daily_raw: FieldFamilyCoverage
    daily_qfq: FieldFamilyCoverage
    is_st: FieldFamilyCoverage
    industry: FieldFamilyCoverage
    qualification: FieldFamilyCoverage
    hard_filter: FieldFamilyCoverage
    risk_facts: FieldFamilyCoverage


@dataclass(frozen=True)
class StockArchivePlan:
    code: str
    existing_first_date: date | None
    existing_last_date: date | None
    expected_active_dates: int
    reusable_daily_cells: int
    missing_raw_dates: tuple[date, ...]
    missing_qfq_dates: tuple[date, ...]
    missing_is_st_dates: tuple[date, ...]
    missing_industry_dates: tuple[date, ...]


@dataclass(frozen=True)
class ArchiveIncrementPlan:
    parent_manifest_hash: str
    parent_manifest_file_hash: str
    parent_logical_records_hash: str
    parent_source_cutoff: date
    target_source_cutoff: date
    active_calendar_dates: tuple[date, ...]
    partition_count: int
    universe_count: int
    stocks: tuple[StockArchivePlan, ...]
    field_coverage: ArchiveFieldCoverage
    estimated_requests: ArchiveRequestEstimate
    training_input_hash: str | None
    training_input_file_hash: str | None
    point_in_time_parity: bool = False
    production_authority: bool = False


@dataclass(frozen=True)
class _Security:
    code: str
    listed_on: date
    delisted_on: date | None


@dataclass(frozen=True)
class _Existing:
    first_date: date | None
    last_date: date | None
    row_count: int
    complete_count: int
    observed_dates: frozenset[date] | None = None


@dataclass(frozen=True)
class _Context:
    source_cutoff: date
    sessions: int
    calendar: tuple[date, ...]
    universe: tuple[_Security, ...]


@dataclass(frozen=True)
class _ArchiveIndexes:
    existing: dict[str, _Existing]
    missing_sides: dict[str, tuple[set[date], set[date]]]
    missing_facts: dict[str, set[date]]
    industry_intervals: dict[str, tuple[tuple[date, date | None], ...]]


@dataclass(frozen=True)
class _PartitionDailyIndexes:
    existing: dict[str, _Existing]
    missing_sides: dict[str, tuple[set[date], set[date]]]
    missing_facts: dict[str, set[date]]


@dataclass(frozen=True)
class _DailyScanContext:
    bounds: tuple[str, str]
    active_dates: tuple[date, ...]
    securities: dict[str, _Security]
    expected_counts: dict[str, int]
    parent_cutoff: date


def build_archive_plan(
    archive_root: Path,
    *,
    target_open_dates: tuple[date, ...],
    training_input_path: Path | None = None,
) -> ArchiveIncrementPlan:
    """Inspect a sealed archive without opening any SQLite file for writing."""

    root = archive_root.resolve()
    manifest_path = root / "manifest.json"
    manifest = _object(json.loads(manifest_path.read_text(encoding="utf-8")), "manifest")
    parent_hash = _digest(manifest.get("content_hash"), "manifest content hash")
    logical_hash = _digest(manifest.get("logical_records_hash"), "logical records hash")
    partitions = _partition_paths(root, manifest.get("partitions"))
    if not partitions:
        raise ValueError("parent archive contains no partitions")
    context = _read_context(partitions[0])
    target = tuple(sorted(set(target_open_dates)))
    if not target or target_open_dates != target:
        raise ValueError("target calendar must be non-empty, unique, and ordered")
    if not set(context.calendar).issubset(target):
        raise ValueError("target calendar must retain the parent calendar before rolling-window trimming")
    active_dates = target[-context.sessions :]
    if active_dates[-1] < context.source_cutoff:
        raise ValueError("target source cutoff cannot precede the parent archive")

    existing, missing_sides, missing_facts = _read_daily_indexes(
        partitions,
        active_dates,
        context.universe,
        context.source_cutoff,
    )
    intervals = _read_industry_intervals(partitions[0])
    indexes = _ArchiveIndexes(existing, missing_sides, missing_facts, intervals)
    stocks = tuple(_stock_plan(security, context, active_dates, indexes) for security in context.universe)
    total = sum(item.expected_active_dates for item in stocks)
    raw_missing = sum(len(item.missing_raw_dates) for item in stocks)
    qfq_missing = sum(len(item.missing_qfq_dates) for item in stocks)
    fact_missing = sum(len(item.missing_is_st_dates) for item in stocks)
    industry_missing = sum(len(item.missing_industry_dates) for item in stocks)
    fields = ArchiveFieldCoverage(
        FieldFamilyCoverage(total - raw_missing, raw_missing),
        FieldFamilyCoverage(total - qfq_missing, qfq_missing),
        FieldFamilyCoverage(total - fact_missing, fact_missing),
        FieldFamilyCoverage(
            total - industry_missing, industry_missing, "effective_at_unavailable" if industry_missing else None
        ),
        FieldFamilyCoverage(0, total, "historical_effective_at_source_unavailable"),
        FieldFamilyCoverage(0, total, "historical_effective_at_source_unavailable"),
        FieldFamilyCoverage(0, total, "historical_published_at_source_unavailable"),
    )
    estimates = ArchiveRequestEstimate(
        sum(bool(item.missing_raw_dates) for item in stocks),
        sum(bool(item.missing_qfq_dates) for item in stocks),
        sum(bool(item.missing_is_st_dates) and not item.missing_raw_dates for item in stocks),
        int(any(day > context.source_cutoff for day in active_dates)),
        2,
    )
    training_path = training_input_path or _default_training_input(root)
    training_hash, training_file_hash = _training_hashes(training_path)
    return ArchiveIncrementPlan(
        parent_hash,
        _file_sha256(manifest_path),
        logical_hash,
        context.source_cutoff,
        active_dates[-1],
        active_dates,
        len(partitions),
        len(context.universe),
        stocks,
        fields,
        estimates,
        training_hash,
        training_file_hash,
    )


def _stock_plan(
    security: _Security,
    context: _Context,
    active_dates: tuple[date, ...],
    indexes: _ArchiveIndexes,
) -> StockArchivePlan:
    expected = _eligible_dates(security, active_dates)
    parent_expected = tuple(day for day in expected if day <= context.source_cutoff)
    observed = indexes.existing.get(security.code, _Existing(None, None, 0, 0, frozenset()))
    missing_cells = (
        set() if observed.observed_dates is None else set(parent_expected).difference(observed.observed_dates)
    )
    raw_exceptions, qfq_exceptions = indexes.missing_sides.get(security.code, (set(), set()))
    incremental = set(expected).difference(parent_expected)
    return StockArchivePlan(
        security.code,
        observed.first_date,
        observed.last_date,
        len(expected),
        observed.complete_count,
        tuple(sorted(incremental | missing_cells | raw_exceptions)),
        tuple(sorted(incremental | missing_cells | qfq_exceptions)),
        tuple(sorted(incremental | missing_cells | indexes.missing_facts.get(security.code, set()))),
        tuple(day for day in expected if not _covered(day, indexes.industry_intervals.get(security.code, ()))),
    )


def _read_context(path: Path) -> _Context:
    with _read_only(path) as connection:
        row = connection.execute(
            "SELECT spec_json, calendar_json, universe_json FROM context WHERE singleton=1"
        ).fetchone()
    if row is None:
        raise ValueError("parent partition context is missing")
    spec = _object(json.loads(cast(str, row[0])), "spec")
    calendar = _object(json.loads(cast(str, row[1])), "calendar")
    universe = _list(json.loads(cast(str, row[2])), "universe")
    securities = []
    for item in universe:
        raw = _object(item, "security")
        securities.append(
            _Security(
                _string(raw.get("code"), "security code"),
                date.fromisoformat(_string(raw.get("listed_on"), "listed_on")),
                _optional_date(raw.get("delisted_on")),
            )
        )
    return _Context(
        date.fromisoformat(_string(spec.get("source_cutoff"), "source cutoff")),
        _integer(spec.get("sessions"), "sessions"),
        tuple(date.fromisoformat(item) for item in _strings(calendar.get("open_dates"), "calendar")),
        tuple(sorted(securities, key=lambda item: item.code)),
    )


def _read_daily_indexes(
    partitions: tuple[Path, ...],
    active_dates: tuple[date, ...],
    universe: tuple[_Security, ...],
    parent_cutoff: date,
) -> tuple[dict[str, _Existing], dict[str, tuple[set[date], set[date]]], dict[str, set[date]]]:
    existing: dict[str, _Existing] = {}
    missing_sides: dict[str, tuple[set[date], set[date]]] = {}
    missing_facts: dict[str, set[date]] = {}
    securities = {item.code: item for item in universe}
    expected_counts = {item.code: len(_eligible_dates(item, active_dates, through=parent_cutoff)) for item in universe}
    bounds = (active_dates[0].isoformat(), active_dates[-1].isoformat())
    scan = _DailyScanContext(bounds, active_dates, securities, expected_counts, parent_cutoff)
    for path in partitions:
        with _read_only(path) as connection:
            current = _read_partition_daily_indexes(connection, scan)
        duplicates = set(existing).intersection(current.existing)
        if duplicates:
            raise ValueError(f"code appears in multiple parent partitions: {min(duplicates)}")
        existing.update(current.existing)
        missing_sides.update(current.missing_sides)
        missing_facts.update(current.missing_facts)
    return existing, missing_sides, missing_facts


def _read_partition_daily_indexes(
    connection: sqlite3.Connection,
    scan: _DailyScanContext,
) -> _PartitionDailyIndexes:
    existing, mismatched = _read_partition_existing(connection, scan.bounds, scan.securities, scan.expected_counts)
    observed = _read_observed_dates(connection, scan.bounds, mismatched)
    for code, dates in observed.items():
        expected = frozenset(_eligible_dates(scan.securities[code], scan.active_dates, through=scan.parent_cutoff))
        if not dates.issubset(expected):
            raise ValueError(f"sealed parent contains an ineligible date for {code}")
        value = existing[code]
        existing[code] = _Existing(
            value.first_date,
            value.last_date,
            value.row_count,
            value.complete_count,
            dates,
        )
    return _PartitionDailyIndexes(
        existing,
        _read_missing_sides(connection, scan.bounds),
        _read_missing_facts(connection, scan.bounds),
    )


def _read_partition_existing(
    connection: sqlite3.Connection,
    bounds: tuple[str, str],
    securities: dict[str, _Security],
    expected_counts: dict[str, int],
) -> tuple[dict[str, _Existing], set[str]]:
    rows = connection.execute(
        "SELECT code, MIN(trade_date), MAX(trade_date), "
        "SUM((trade_date BETWEEN ? AND ?)), "
        "SUM((trade_date BETWEEN ? AND ?) AND "
        "COALESCE(json_type(payload_json, '$.unadjusted'), 'null') <> 'null' "
        "AND COALESCE(json_type(payload_json, '$.qfq'), 'null') <> 'null') FROM daily_cells GROUP BY code",
        (*bounds, *bounds),
    ).fetchall()
    existing: dict[str, _Existing] = {}
    mismatched: set[str] = set()
    for row in rows:
        code = cast(str, row[0])
        if code not in securities:
            raise ValueError(f"parent partition contains a code outside the frozen universe: {code}")
        row_count = int(row[3])
        if row_count != expected_counts[code]:
            mismatched.add(code)
        existing[code] = _Existing(
            date.fromisoformat(cast(str, row[1])),
            date.fromisoformat(cast(str, row[2])),
            row_count,
            int(row[4]),
        )
    return existing, mismatched


def _read_observed_dates(
    connection: sqlite3.Connection,
    bounds: tuple[str, str],
    codes: set[str],
) -> dict[str, frozenset[date]]:
    if not codes:
        return {}
    placeholders = ",".join("?" for _code in codes)
    query = f"SELECT code, trade_date FROM daily_cells WHERE trade_date BETWEEN ? AND ? AND code IN ({placeholders})"
    observed: dict[str, set[date]] = {code: set() for code in codes}
    for code_value, day_value in connection.execute(query, (*bounds, *sorted(codes))):
        observed[cast(str, code_value)].add(date.fromisoformat(cast(str, day_value)))
    return {code: frozenset(dates) for code, dates in observed.items()}


def _read_missing_sides(
    connection: sqlite3.Connection,
    bounds: tuple[str, str],
) -> dict[str, tuple[set[date], set[date]]]:
    rows = connection.execute(
        "SELECT code, trade_date, COALESCE(json_type(payload_json, '$.unadjusted'), 'null') = 'null', "
        "COALESCE(json_type(payload_json, '$.qfq'), 'null') = 'null' FROM daily_cells "
        "WHERE trade_date BETWEEN ? AND ? AND "
        "(COALESCE(json_type(payload_json, '$.unadjusted'), 'null') = 'null' "
        "OR COALESCE(json_type(payload_json, '$.qfq'), 'null') = 'null')",
        bounds,
    ).fetchall()
    missing: dict[str, tuple[set[date], set[date]]] = {}
    for code_value, day_value, raw_missing, qfq_missing in rows:
        raw_dates, qfq_dates = missing.setdefault(cast(str, code_value), (set(), set()))
        day = date.fromisoformat(cast(str, day_value))
        if raw_missing:
            raw_dates.add(day)
        if qfq_missing:
            qfq_dates.add(day)
    return missing


def _read_missing_facts(connection: sqlite3.Connection, bounds: tuple[str, str]) -> dict[str, set[date]]:
    rows = connection.execute(
        "SELECT cells.code, cells.trade_date FROM daily_cells AS cells LEFT JOIN daily_facts AS facts "
        "ON facts.code=cells.code AND facts.trade_date=cells.trade_date "
        "WHERE cells.trade_date BETWEEN ? AND ? AND facts.code IS NULL",
        bounds,
    ).fetchall()
    missing: dict[str, set[date]] = {}
    for code_value, day_value in rows:
        missing.setdefault(cast(str, code_value), set()).add(date.fromisoformat(cast(str, day_value)))
    return missing


def _eligible_dates(
    security: _Security,
    active_dates: tuple[date, ...],
    *,
    through: date | None = None,
) -> tuple[date, ...]:
    return tuple(
        day
        for day in active_dates
        if day >= security.listed_on
        and (security.delisted_on is None or day < security.delisted_on)
        and (through is None or day <= through)
    )


def _read_industry_intervals(path: Path) -> dict[str, tuple[tuple[date, date | None], ...]]:
    with _read_only(path) as connection:
        rows = connection.execute("SELECT code, effective_from, effective_to FROM industry_intervals").fetchall()
    values: dict[str, list[tuple[date, date | None]]] = {}
    for code_value, start_value, end_value in rows:
        values.setdefault(cast(str, code_value), []).append(
            (date.fromisoformat(cast(str, start_value)), _optional_date(end_value))
        )
    return {code: tuple(sorted(items)) for code, items in values.items()}


def _covered(day: date, intervals: tuple[tuple[date, date | None], ...]) -> bool:
    return any(start <= day and (end is None or day < end) for start, end in intervals)


def _partition_paths(root: Path, value: object) -> tuple[Path, ...]:
    paths = []
    for item in _list(value, "partitions"):
        relative = Path(_string(_object(item, "partition").get("relative_path"), "partition path"))
        path = (root / relative).resolve()
        if root not in path.parents or not path.is_file():
            raise ValueError("parent partition path is invalid")
        paths.append(path)
    return tuple(paths)


def _read_only(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path.as_posix()}?mode=ro&immutable=1", uri=True)


def _default_training_input(root: Path) -> Path | None:
    data_root = next((parent for parent in root.parents if parent.name == "data"), root)
    output_root = data_root / "train" / "tomorrow-v3"
    if not (output_root / "active-bundle.json").is_file():
        return None
    return locate_active_tomorrow_bundle(output_root).with_name("training-input.json")


def _training_hashes(path: Path | None) -> tuple[str | None, str | None]:
    if path is None or not path.is_file():
        return None, None
    raw = _object(json.loads(path.read_text(encoding="utf-8")), "training input")
    value = raw.get("training_input_hash")
    return (value if isinstance(value, str) and len(value) == 64 else None, _file_sha256(path))


def _object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise TypeError(f"{label} is not an object")
    return cast(dict[str, object], value)


def _list(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise TypeError(f"{label} is not a list")
    return value


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise TypeError(f"{label} is not a string")
    return value


def _strings(value: object, label: str) -> tuple[str, ...]:
    values = _list(value, label)
    if not all(isinstance(item, str) for item in values):
        raise TypeError(f"{label} contains a non-string")
    return tuple(cast(str, item) for item in values)


def _integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise TypeError(f"{label} is not a positive integer")
    return value


def _optional_date(value: object) -> date | None:
    return None if value is None else date.fromisoformat(_string(value, "optional date"))


def _digest(value: object, label: str) -> str:
    text = _string(value, label)
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise ValueError(f"{label} is invalid")
    return text


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


__all__ = [
    "ArchiveFieldCoverage",
    "ArchiveIncrementPlan",
    "ArchiveRequestEstimate",
    "FieldFamilyCoverage",
    "StockArchivePlan",
    "build_archive_plan",
]
