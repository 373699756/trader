"""Bounded, source-evidenced percentage diagnostics; never a repair or full price audit."""

from __future__ import annotations

import math
import re
import sqlite3
from collections import Counter
from contextlib import ExitStack
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from typing import Literal

from scripts.download.history.history_publication_audit import (
    CapturePriceAuditSources,
    PriceAuditBudget,
    PriceAuditInterrupted,
    PriceAuditSource,
    PriceAuditSourcesUnchanged,
)
from trader.infra.sqlite_codec import DecodeDate
from trader.infra.sqlite_connection import SqliteConnection, SqliteConnectionOptions, SqliteSession, SqliteTransaction
from trader.infra.sqlite_policy import SqliteIoPolicy

_MAX_PRICE_ROWS = 512
_MAX_SOURCE_ROWS = 512
_RELATIVE_TOLERANCE = 0.00001
_ABSOLUTE_TOLERANCE_PCT = 0.0001
_CODE = re.compile(r"[0-9]{6}")
_RAW_FIELDS = "raw_close,raw_preclose,raw_change_pct,raw_turnover_pct"


@dataclass(frozen=True)
class _SourceEvidence:
    dataset: Literal["history", "market"]
    code: str
    day: date
    evidence: str
    source: str
    publication: str
    sequence: int | None = None
    observation_sequence: int | None = None
    revision: str | None = None


@dataclass(frozen=True)
class _PriceEvidence:
    identity: _SourceEvidence
    database: str
    raw_close: float | None
    raw_preclose: float | None
    stored_pct: float | None
    stored_turnover: float | None
    implied_pct: float | None
    assessment: str
    reason: str


class _Readers:
    def __init__(self, stack: ExitStack, budget: PriceAuditBudget, policy: SqliteIoPolicy) -> None:
        self.budget = budget
        self._policy = policy
        self._stack = stack
        self.connections: dict[Path, SqliteConnection] = {}
        self.sources: list[PriceAuditSource] = []

    def open(self, path: Path) -> SqliteConnection:
        self.budget.check()
        if path in self.connections:
            return self.connections[path]
        source, = CapturePriceAuditSources((path,), self.budget)
        self.sources.append(source)
        policy = replace(self._policy, connection_timeout_seconds=min(
            self._policy.connection_timeout_seconds, 0.2, max(0.001, self.budget.deadline - self.budget.clock()),
        ))
        connection = self._stack.enter_context(SqliteSession(
            path,
            SqliteConnectionOptions(read_only=True, query_only=True, immutable=source.wal is None or source.wal.size == 0),
            sqlite_policy=policy,
        ))
        self.connections[path] = connection
        self._stack.enter_context(SqliteTransaction(connection))
        connection.observe_progress(self.budget.progress, 1000)
        return connection


def _number(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def _classify(identity: _SourceEvidence, path: Path, values: tuple[object, ...] | None) -> _PriceEvidence:
    close, preclose, stored, turnover = tuple(_number(item) for item in values) if values is not None else (None,) * 4
    implied = _number((close / preclose - 1) * 100) if close is not None and close > 0 and preclose is not None and preclose > 0 else None
    assessment = "unassessable"
    if values is None:
        reason = "published_price_missing"
    elif close is None or close <= 0:
        reason = "close_missing_or_invalid"
    elif preclose is None or preclose <= 0:
        reason = "preclose_missing_or_invalid"
    elif stored is None:
        reason = "stored_pct_missing_or_invalid"
    elif implied is None or not math.isfinite(implied):
        reason = "raw_return_not_finite"
    elif stored == 0 or implied == 0:
        reason = "zero_change_cannot_identify_unit"
    else:
        normal = math.isclose(stored, implied, rel_tol=_RELATIVE_TOLERANCE, abs_tol=_ABSOLUTE_TOLERANCE_PCT)
        shrunk = math.isclose(stored * 100, implied, rel_tol=_RELATIVE_TOLERANCE, abs_tol=_ABSOLUTE_TOLERANCE_PCT)
        if normal and shrunk:
            reason = "unit_ambiguous_at_tolerance"
        else:
            assessment = "suspected_shrunk_100x" if shrunk else "consistent" if normal else "other_mismatch"
            reason = "stored_times_100_matches_raw_return" if shrunk else "raw_return_comparison"
    return _PriceEvidence(identity, path.name, close, preclose, stored, turnover, implied, assessment, reason)


def _identity(code: object, day: object) -> tuple[str, date]:
    if not isinstance(code, str) or _CODE.fullmatch(code) is None:
        raise ValueError("percentage_audit_code_invalid")
    return code, DecodeDate(day)


def _history(
    root: Path, readers: _Readers, samples: list[_PriceEvidence], unvisited: list[str],
) -> None:
    readers.budget.phase = "percentage_history_receipts"
    catalog = readers.open(root / "catalog.sqlite3")
    published = catalog.run(
        "SELECT p.sync_sequence,hex(p.snapshot_hash) FROM published_snapshot p JOIN history_snapshot s "
        "ON s.id=p.sync_sequence AND s.content_hash=p.snapshot_hash WHERE p.singleton=1"
    ).fetchone()
    if published is None:
        raise ValueError("percentage_audit_history_publication_missing")
    for position, row in enumerate(catalog.run(
        "SELECT code,trade_date FROM price_repair_attempt WHERE state='resolved' AND resolved_source='baostock' "
        "ORDER BY code,trade_date LIMIT ?", (_MAX_SOURCE_ROWS + 1,),
    )):
        readers.budget.check()
        if position == _MAX_SOURCE_ROWS or len(samples) == _MAX_PRICE_ROWS:
            unvisited.append("history_baostock_receipts_beyond_fixed_limit")
            break
        code, day = _identity(row[0], row[1])
        readers.budget.phase = "percentage_history_published_price"
        partition = catalog.run(
            "SELECT relative_path,sync_sequence FROM snapshot_partition "
            "WHERE snapshot_id=? AND calendar_year=? AND calendar_month=?", (published[0], day.year, day.month),
        ).fetchone()
        if partition is None:
            raise ValueError("percentage_audit_history_partition_missing")
        if partition[0] != f"prices_{day.year:04d}.sqlite3" or type(partition[1]) is not int or not 0 < partition[1] <= published[0]:
            raise ValueError("percentage_audit_history_partition_invalid")
        path = root / partition[0]
        connection = readers.open(path)
        price = connection.run(
            f"SELECT o.sync_sequence,o.revision_id,{_RAW_FIELDS} FROM daily_price_observation o "
            "INDEXED BY history_price_observation_code_date_idx JOIN daily_price_revision r "
            "ON r.trade_date=o.trade_date AND r.code=o.code AND r.revision_id=o.revision_id "
            "WHERE o.code=? AND o.trade_date=? AND o.sync_sequence<=? ORDER BY o.sync_sequence DESC LIMIT 1",
            (code, row[1], partition[1]),
        ).fetchone()
        identity = _SourceEvidence(
            "history", code, day, "resolved_baostock_repair_receipt", "baostock", str(published[1]).lower(),
            int(partition[1]), int(price[0]) if price is not None else None, str(price[1]) if price is not None else None,
        )
        samples.append(_classify(identity, path, tuple(price[2:]) if price is not None else None))


def _market(
    path: Path, readers: _Readers, samples: list[_PriceEvidence], unvisited: list[str],
) -> None:
    readers.budget.phase = "percentage_market_receipts"
    if len(samples) == _MAX_PRICE_ROWS:
        unvisited.append("market_not_visited_price_limit")
        return
    connection = readers.open(path)
    seen: set[tuple[str, int]] = set()
    # Receipt evidence remains explicit; it does not prove later overwrites kept the same supplier.
    for position, row in enumerate(connection.run(
        "SELECT code,trade_date FROM price_repair_attempt WHERE state='resolved' AND resolved_source='baostock' "
        "ORDER BY code,trade_date LIMIT ?", (_MAX_SOURCE_ROWS + 1,),
    )):
        readers.budget.check()
        if position == _MAX_SOURCE_ROWS or len(samples) == _MAX_PRICE_ROWS:
            unvisited.append("market_baostock_receipts_beyond_fixed_limit")
            break
        code, day = _identity(row[0], row[1])
        binding = connection.run(
            "SELECT b.source,p.publication_id FROM window_binding b JOIN window_publication p ON p.profile=b.profile "
            "JOIN window_population m ON m.profile=b.profile AND m.code=b.code "
            "WHERE b.profile IN ('v2','v3') AND b.code=? AND ? BETWEEN b.start_date AND b.end_date "
            "ORDER BY b.profile LIMIT 1", (code, row[1]),
        ).fetchone()
        if binding is None:
            unvisited.append(f"market_receipt_outside_published_window:{code}:{day.isoformat()}")
            continue
        price = connection.run(f"SELECT {_RAW_FIELDS} FROM daily_price WHERE code=? AND trade_date=?", (code, row[1])).fetchone()
        identity = _SourceEvidence("market", code, day, "resolved_baostock_repair_receipt", str(binding[0]), str(binding[1]))
        samples.append(_classify(identity, path, tuple(price) if price is not None else None))
        seen.add((code, row[1]))
    readers.budget.phase = "percentage_market_baostock_windows"
    for position, binding in enumerate(connection.run(
        "SELECT b.code,b.source,b.start_date,b.end_date,p.publication_id FROM window_binding b "
        "JOIN window_publication p ON p.profile=b.profile JOIN window_population m ON m.profile=b.profile AND m.code=b.code "
        "WHERE b.source GLOB 'baostock:*' ORDER BY b.profile,b.code LIMIT ?", (_MAX_SOURCE_ROWS + 1,),
    )):
        readers.budget.check()
        if position == _MAX_SOURCE_ROWS or len(samples) == _MAX_PRICE_ROWS:
            unvisited.append("market_baostock_windows_beyond_fixed_limit")
            break
        code, _ = _identity(binding[0], binding[2])
        for price in connection.run(
            f"SELECT trade_date,{_RAW_FIELDS} FROM daily_price WHERE code=? AND trade_date BETWEEN ? AND ? "
            "ORDER BY trade_date LIMIT ?", (code, binding[2], binding[3], _MAX_PRICE_ROWS + 1),
        ):
            readers.budget.check()
            if (code, price[0]) in seen:
                continue
            if len(samples) == _MAX_PRICE_ROWS:
                unvisited.append("market_baostock_window_prices_beyond_fixed_limit")
                return
            identity = _SourceEvidence("market", code, DecodeDate(price[0]), "explicit_baostock_window_binding",
                                       str(binding[1]), str(binding[4]))
            samples.append(_classify(identity, path, tuple(price[1:])))
            seen.add((code, price[0]))


def AuditPricePercentageUnits(
    history_root: Path, *, market_path: Path, sqlite_policy: SqliteIoPolicy, budget: PriceAuditBudget,
) -> dict[str, object]:
    """Inspect at most 512 named published prices; never infer a population-wide impact count."""
    if not 0 < budget.seconds <= 15:
        raise ValueError("percentage_audit_budget_must_not_exceed_15_seconds")
    samples: list[_PriceEvidence] = []
    unvisited = ["prices_without_explicit_baostock_source_evidence", "full_price_population_not_scanned"]
    failures: list[str] = []
    interrupted: str | None = None
    with ExitStack() as stack:
        readers = _Readers(stack, budget, sqlite_policy)
        try:
            _history(history_root, readers, samples, unvisited)
            _market(market_path, readers, samples, unvisited)
        except PriceAuditInterrupted as exc:
            interrupted = str(exc)
            unvisited.append("remaining_evidence_not_visited:" + interrupted)
        except (OSError, ValueError, sqlite3.Error) as exc:
            if isinstance(exc, sqlite3.OperationalError) and budget.interrupted is not None:
                interrupted = budget.interrupted
                unvisited.append("remaining_evidence_not_visited:" + interrupted)
            else:
                failures.append(str(exc) if isinstance(exc, ValueError) else type(exc).__name__)
                unvisited.append("remaining_evidence_not_visited:" + budget.phase)
        finally:
            # Disable deadline callbacks before closing the read transactions, retaining already collected evidence.
            for connection in readers.connections.values():
                connection.observe_progress(None, 0)
            changes = sum(connection.total_changes for connection in readers.connections.values())
    sources = tuple(readers.sources)
    unchanged = PriceAuditSourcesUnchanged(sources)
    if not unchanged:
        failures.append("percentage_audit_source_changed")
    if changes:
        failures.append("percentage_audit_unexpected_source_changes")
    counts = Counter(item.assessment for item in samples)
    incomplete = interrupted is not None or len(unvisited) > 2 or not samples
    return {
        "status": "failed" if failures else "degraded" if incomplete or counts["suspected_shrunk_100x"] or counts["other_mismatch"] or counts["unassessable"] else "passed",
        "summary": {
            "scope": "bounded_baostock_source_evidence_not_full_price_population",
            "read_only": True, "source_data_writes": changes, "network_requests": 0,
            "percentage_unit_audit": {
                "full_population_verified": False, "fixed_price_limit": _MAX_PRICE_ROWS,
                "fixed_source_row_limit": _MAX_SOURCE_ROWS, "examined_prices": len(samples),
                "assessments": dict(counts), "budget_seconds": budget.seconds, "interrupted": interrupted,
                "unvisited": unvisited, "failures": failures, "source_stat_unchanged": unchanged,
                "sources_before": [
                    {
                        "path": source.path,
                        "database": {
                            "device": source.database.device, "inode": source.database.inode,
                            "size": source.database.size, "modified_ns": source.database.modified_ns,
                            "changed_ns": source.database.changed_ns,
                        },
                        "wal": {
                            "device": source.wal.device, "inode": source.wal.inode,
                            "size": source.wal.size, "modified_ns": source.wal.modified_ns,
                            "changed_ns": source.wal.changed_ns,
                        } if source.wal is not None else None,
                    }
                    for source in sources
                ],
                "comparison": {"relative_tolerance": _RELATIVE_TOLERANCE, "absolute_tolerance_pct": _ABSOLUTE_TOLERANCE_PCT},
                "turnover_assessment": "source_evidence_only_no_numeric_rescaling_basis",
                "receipt_source_does_not_prove_no_later_supplier_overwrite": True,
                "samples": [
                    {
                        "identity": {
                            "dataset": item.identity.dataset, "code": item.identity.code,
                            "day": item.identity.day.isoformat(), "year": item.identity.day.year,
                            "evidence": item.identity.evidence, "source": item.identity.source,
                            "publication": item.identity.publication, "sequence": item.identity.sequence,
                            "observation_sequence": item.identity.observation_sequence, "revision": item.identity.revision,
                        },
                        "database": item.database, "raw_close": item.raw_close, "raw_preclose": item.raw_preclose,
                        "stored_pct": item.stored_pct, "stored_turnover": item.stored_turnover,
                        "implied_pct": item.implied_pct, "assessment": item.assessment, "reason": item.reason,
                    }
                    for item in samples
                ],
            },
        },
    }
