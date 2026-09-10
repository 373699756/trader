"""Typed training rows read from one verified BaoStock active archive."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal, TypeVar, cast

from trader.domain.research.baostock_active_archive import (
    BaoStockActiveManifest,
    BaoStockArchiveFieldFamily,
    BaoStockArchiveRecordKey,
)
from trader.domain.research.baostock_daily import (
    BaoStockBoard,
    BaoStockCalendar,
    BaoStockDailySide,
    BaoStockTrainingRow,
)
from trader.domain.research.tomorrow_training_input import FrozenDailyInputDescriptor
from trader.infra.research.baostock_active_archive import (
    BaoStockActiveArchive,
    BaoStockActiveArchiveConflictError,
    BaoStockIncrementRecord,
)
from trader.infra.research.baostock_daily import (
    BaoStockDailyArtifactConflictError,
    BaoStockDailyPartitionedArchive,
)

_T = TypeVar("_T")


@dataclass(frozen=True)
class BaoStockActiveTrainingInputSnapshot:
    input_scope: Literal["complete_manifest"]
    input_hash: str
    parent_manifest_hash: str
    increment_manifest_hash: str
    calendar_hash: str
    source_cutoff: date
    calendar: BaoStockCalendar
    training_codes: tuple[str, ...]
    input_descriptor_hash: str

    @property
    def universe_count(self) -> int:
        return len(self.training_codes)


class BaoStockActiveTrainingInputArchive:
    """Read a parent-plus-increment generation after its active identity is verified."""

    def __init__(
        self,
        root: Path,
        active: BaoStockActiveArchive,
        manifest: BaoStockActiveManifest,
        descriptor: FrozenDailyInputDescriptor,
        snapshot: BaoStockActiveTrainingInputSnapshot,
    ) -> None:
        self._root = root
        self._active = active
        self._manifest = manifest
        self._descriptor = descriptor
        self._training_codes = frozenset(snapshot.training_codes)
        self.snapshot = snapshot

    @classmethod
    def open(cls, root: Path) -> BaoStockActiveTrainingInputArchive:
        if not (root / "manifest.json").is_file() or not (root / "active-manifest.json").is_file():
            raise BaoStockActiveArchiveConflictError("BaoStock history manifest is unavailable")
        try:
            parent = BaoStockDailyPartitionedArchive(root).verify()
            active = BaoStockActiveArchive.open(root)
            manifest = active.verify()
        except BaoStockDailyArtifactConflictError as exc:
            raise BaoStockActiveArchiveConflictError("BaoStock active training parent is invalid") from exc
        if parent.content_hash != manifest.parent_manifest_hash:
            raise BaoStockActiveArchiveConflictError("BaoStock active training parent identity is invalid")
        descriptor = active.describe_frozen_daily_input(manifest)
        snapshot = BaoStockActiveTrainingInputSnapshot(
            "complete_manifest",
            manifest.active_data_hash,
            manifest.parent_manifest_hash,
            manifest.increment_manifest_hash,
            manifest.calendar_hash,
            manifest.source_cutoff,
            active.active_calendar,
            active.universe_codes,
            descriptor.content_hash,
        )
        return cls(root, active, manifest, descriptor, snapshot)

    def describe_frozen_daily_input(self) -> FrozenDailyInputDescriptor:
        return self._descriptor

    def read_training_rows(
        self,
        code: str,
        *,
        allowed_dates: frozenset[date],
    ) -> tuple[BaoStockTrainingRow, ...]:
        if code not in self._training_codes:
            raise ValueError("BaoStock code is outside the active training input")
        partition = self._active.context.partition_for(code)
        parent_path = self._root / "shards" / f"{partition}.sqlite3"
        increment_root = (self._root / self._manifest.increment_manifest_path).parent
        increment_path = increment_root / "shards" / f"increment-{partition}.sqlite3"
        try:
            parent = _read_parent(parent_path, code, allowed_dates)
            increment = _read_increment(increment_path, code, allowed_dates)
            return _merge_rows(code, _board_from_partition(partition), parent, increment, allowed_dates)
        except (json.JSONDecodeError, sqlite3.DatabaseError, TypeError, ValueError) as exc:
            raise BaoStockActiveArchiveConflictError("BaoStock active training row is invalid") from exc


@dataclass(frozen=True)
class _LayerRows:
    raw: tuple[tuple[date, BaoStockDailySide], ...]
    qfq: tuple[tuple[date, BaoStockDailySide], ...]
    is_st: tuple[tuple[date, bool], ...]
    industries: tuple[tuple[date, date | None, str], ...]


def _read_parent(path: Path, code: str, allowed_dates: frozenset[date]) -> _LayerRows:
    if not path.is_file():
        raise ValueError("BaoStock active parent partition is missing")
    with sqlite3.connect(f"file:{path.as_posix()}?mode=ro&immutable=1", uri=True) as connection:
        cells = connection.execute(
            "SELECT trade_date, payload_json FROM daily_cells WHERE code=? ORDER BY trade_date", (code,)
        ).fetchall()
        facts = connection.execute(
            "SELECT trade_date, is_st FROM daily_facts WHERE code=? ORDER BY trade_date", (code,)
        ).fetchall()
        intervals = connection.execute(
            "SELECT effective_from, effective_to, industry FROM industry_intervals "
            "WHERE code=? ORDER BY effective_from",
            (code,),
        ).fetchall()
    raw: list[tuple[date, BaoStockDailySide]] = []
    qfq: list[tuple[date, BaoStockDailySide]] = []
    for stored_day, payload_json in cells:
        day = date.fromisoformat(cast(str, stored_day))
        if day not in allowed_dates:
            continue
        payload = _object(json.loads(cast(str, payload_json)), "parent daily cell")
        for name, target in (("unadjusted", raw), ("qfq", qfq)):
            value = payload.get(name)
            if value is not None:
                target.append((day, _decode_side(_object(value, name))))
    return _LayerRows(
        tuple(raw),
        tuple(qfq),
        tuple(
            (date.fromisoformat(cast(str, day)), bool(value))
            for day, value in facts
            if date.fromisoformat(cast(str, day)) in allowed_dates and value in (0, 1)
        ),
        tuple(
            (
                date.fromisoformat(cast(str, start)),
                date.fromisoformat(cast(str, end)) if end is not None else None,
                cast(str, industry),
            )
            for start, end, industry in intervals
        ),
    )


def _read_increment(path: Path, code: str, allowed_dates: frozenset[date]) -> _LayerRows:
    if not path.is_file():
        return _LayerRows((), (), (), ())
    with sqlite3.connect(f"file:{path.as_posix()}?mode=ro&immutable=1", uri=True) as connection:
        rows = connection.execute(
            "SELECT trade_date, field_family, payload_json, content_hash FROM records "
            "WHERE code=? AND field_family IN ('daily_raw','daily_qfq','is_st','industry') "
            "ORDER BY trade_date, field_family",
            (code,),
        ).fetchall()
    raw: list[tuple[date, BaoStockDailySide]] = []
    qfq: list[tuple[date, BaoStockDailySide]] = []
    facts: list[tuple[date, bool]] = []
    industries: list[tuple[date, date | None, str]] = []
    for stored_day, family, payload_json, stored_hash in rows:
        day = date.fromisoformat(cast(str, stored_day))
        key = BaoStockArchiveRecordKey(code, day, cast(BaoStockArchiveFieldFamily, family))
        record = BaoStockIncrementRecord(key, cast(str, payload_json), cast(str, stored_hash))
        payload = _object(json.loads(record.payload_json), "increment record")
        if family == "daily_raw" and day in allowed_dates:
            raw.append((day, _decode_side(payload)))
        elif family == "daily_qfq" and day in allowed_dates:
            qfq.append((day, _decode_side(payload)))
        elif family == "is_st" and day in allowed_dates:
            value = payload.get("is_st")
            if not isinstance(value, bool):
                raise TypeError("BaoStock active is_st record is invalid")
            facts.append((day, value))
        elif family == "industry":
            end = payload.get("effective_to")
            industries.append(
                (
                    day,
                    date.fromisoformat(end) if isinstance(end, str) else None,
                    _text(payload, "industry"),
                )
            )
    return _LayerRows(tuple(raw), tuple(qfq), tuple(facts), tuple(industries))


def _merge_rows(
    code: str,
    board: BaoStockBoard,
    parent: _LayerRows,
    increment: _LayerRows,
    allowed_dates: frozenset[date],
) -> tuple[BaoStockTrainingRow, ...]:
    raw = _merge_pairs(parent.raw, increment.raw)
    qfq = _merge_pairs(parent.qfq, increment.qfq)
    facts = _merge_pairs(parent.is_st, increment.is_st)
    intervals = tuple(sorted((*parent.industries, *increment.industries)))
    rows: list[BaoStockTrainingRow] = []
    for day in sorted(allowed_dates.intersection(raw, qfq, facts)):
        industry = _industry_on(intervals, day)
        if industry is not None:
            rows.append(BaoStockTrainingRow(code, day, board, industry, facts[day], raw[day], qfq[day]))
    return tuple(rows)


def _merge_pairs(parent: tuple[tuple[date, _T], ...], increment: tuple[tuple[date, _T], ...]) -> dict[date, _T]:
    result = dict(parent)
    for day, value in increment:
        existing = result.get(day)
        if existing is not None and existing != value:
            raise ValueError("BaoStock parent/increment training record conflicts")
        result[day] = value
    return result


def _industry_on(intervals: tuple[tuple[date, date | None, str], ...], day: date) -> str | None:
    matching = tuple((start, value) for start, end, value in intervals if start <= day and (end is None or day < end))
    return max(matching)[1] if matching else None


def _decode_side(payload: dict[str, object]) -> BaoStockDailySide:
    adjustment = _text(payload, "adjustment")
    trading_status = _text(payload, "trading_status")
    return BaoStockDailySide(
        _text(payload, "code"),
        date.fromisoformat(_text(payload, "trade_date")),
        cast(Literal["unadjusted", "qfq"], adjustment),
        _optional_number(payload, "open_price"),
        _optional_number(payload, "high_price"),
        _optional_number(payload, "low_price"),
        _optional_number(payload, "close_price"),
        _optional_number(payload, "volume"),
        _optional_number(payload, "amount"),
        _optional_number(payload, "preclose"),
        _optional_number(payload, "pct_change"),
        _optional_number(payload, "turnover"),
        cast(Literal["trading", "suspended"], trading_status),
    )


def _object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise TypeError(f"BaoStock {label} must be an object")
    return cast(dict[str, object], value)


def _text(payload: dict[str, object], name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value:
        raise TypeError(f"BaoStock {name} must be text")
    return value


def _optional_number(payload: dict[str, object], name: str) -> float | None:
    value = payload.get(name)
    if value is None:
        return None
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise TypeError(f"BaoStock {name} must be numeric")
    return float(value)


def _board_from_partition(partition: str) -> BaoStockBoard:
    board = partition.split("-", 1)[0]
    if board not in {"main", "chinext", "star"}:
        raise ValueError("BaoStock active partition board is invalid")
    return cast(BaoStockBoard, board)


__all__ = ["BaoStockActiveTrainingInputArchive", "BaoStockActiveTrainingInputSnapshot"]
