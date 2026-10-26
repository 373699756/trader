"""Evaluate annual history indexes on a consistent, disposable SQLite copy."""

from __future__ import annotations

import argparse
import calendar
import hashlib
import json
import shutil
import statistics
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass
from datetime import date
from itertools import islice
from pathlib import Path

from trader.download.domain.history_revision import HistoryRevision
from trader.download.infra.history_prices import HistoryPricePartition, HistoryPricePartitionError
from trader.infra.sqlite_connection import SqliteConnection, SqliteConnectionOptions, SqliteSession, SqliteTransaction

from .diagnostic_types import PROJECT_ROOT
from .reporting import emit_report

_CANDIDATE_INDEX = "history_price_date_board_code_idx"
_READ = SqliteConnectionOptions(read_only=True, timeout_seconds=2, cache_kib=8192, mmap_bytes=0, temp_storage="FILE")
_WRITE = SqliteConnectionOptions(
    journal_mode="WAL", timeout_seconds=2, cache_kib=8192, mmap_bytes=0, temp_storage="FILE"
)


@dataclass(frozen=True)
class _Query:
    name: str
    sql: str


@dataclass(frozen=True)
class _ReadMeasurement:
    milliseconds: float
    rows: int
    approximate_vm_steps: int


@dataclass(frozen=True)
class _WriteMeasurement:
    write_ms: float
    rebuild_ms: float
    total_ms: float
    database_bytes: int
    logical_hash: str
    row_count: int


@contextmanager
def _bounded(connection: SqliteConnection) -> Iterator[Callable[[], None]]:
    deadline = time.monotonic() + 60

    def check() -> None:
        if time.monotonic() >= deadline:
            raise TimeoutError("history_index_statement_timeout")

    connection.observe_progress(lambda: int(time.monotonic() >= deadline), 10_000)
    try:
        yield check
        check()
    finally:
        connection.observe_progress(None, 0)


def _copy(source: Path, target: Path) -> None:
    with SqliteSession(source, _READ) as reader, SqliteSession(target) as writer:
        reader.backup_to(writer, deadline=time.monotonic() + 60, monotonic=time.monotonic)


def _select_source(root: Path) -> Path:
    candidates = tuple(root.glob("prices_[0-9][0-9][0-9][0-9].sqlite3"))
    if not candidates or any(path.is_symlink() or not path.is_file() for path in candidates):
        raise ValueError("history_index_source_unavailable")
    # Largest file is a bounded, deterministic sample; publication is not assumed.
    source = max(candidates, key=lambda path: (path.stat().st_size, path.name))
    if source.stat().st_size > 2 * 1024**3:
        raise ValueError("history_index_source_exceeds_2_gib")
    return source


def _inventory(path: Path) -> tuple[list[dict[str, object]], str, int, int]:
    with SqliteSession(path, _READ) as connection, _bounded(connection):
        identity = connection.run("SELECT schema_identity, calendar_year FROM metadata WHERE singleton=1").require_one()
        if identity[0] != "history_annual_prices" or connection.run("PRAGMA user_version").require_one()[0] != 1:
            raise ValueError("history_index_source_schema_unsupported")
        indexes = connection.run(
            "SELECT name, tbl_name, sql FROM sqlite_schema WHERE type='index' AND sql IS NOT NULL ORDER BY name"
        ).fetchall()
        sizes = {
            str(row[0]): int(row[1]) for row in connection.run("SELECT name,SUM(pgsize) FROM dbstat GROUP BY name")
        }
        ddl = next(str(row[2]) for row in indexes if row[0] == _CANDIDATE_INDEX)
        records = int(connection.run("SELECT COUNT(*) FROM daily_records").require_one()[0])
        return (
            [dict(name=row[0], table=row[1], bytes=sizes.get(str(row[0]), 0)) for row in indexes],
            ddl,
            int(identity[1]),
            records,
        )


def _queries(path: Path, year: int, sample_count: int) -> tuple[tuple[_Query, ...], tuple[HistoryRevision, ...], int]:
    with SqliteSession(path, _READ) as connection, _bounded(connection):
        row = connection.run(
            "SELECT trade_date,code,board FROM daily_records ORDER BY trade_date DESC,code LIMIT 1"
        ).require_one()
        end, code, board = date.fromisoformat(str(row[0])), str(row[1]), str(row[2])
        sequence = int(connection.run("SELECT MAX(sync_sequence) FROM daily_observations").require_one()[0])
    if end.year != year:
        raise ValueError("history_index_source_year_conflicts")
    start = end.replace(day=1)
    end = end.replace(day=calendar.monthrange(year, end.month)[1])
    statements: list[str] = []
    partition = HistoryPricePartition(path, year, end.month, statement_trace=statements.append)
    with closing(partition.iter_range(start, end, snapshot_sequence=sequence)) as rows:
        sample = tuple(islice(rows, sample_count))
    if not sample:
        raise ValueError("history_index_source_month_empty")
    operations: tuple[tuple[str, Callable[[], object]], ...] = (
        ("latest_month", lambda: sum(1 for _ in partition.iter_range(start, end, snapshot_sequence=sequence))),
        ("single_code", lambda: partition.read_code(code, start, end, snapshot_sequence=sequence)),
        ("day_board", lambda: partition.read_day(sample[-1].trade_date, snapshot_sequence=sequence, board=board)),
        (
            "code_board",
            lambda: sum(
                1 for _ in partition.iter_range(start, end, snapshot_sequence=sequence, code=code, board=board)
            ),
        ),
        (
            "code_ordered_month",
            lambda: sum(1 for _ in partition.iter_range_by_code(start, end, snapshot_sequence=sequence)),
        ),
        (
            "startup_month",
            lambda: sum(1 for _ in partition.iter_published_cells(start, end, snapshot_sequence=sequence)),
        ),
        (
            "startup_code",
            lambda: sum(1 for _ in partition.iter_published_cells(start, end, snapshot_sequence=sequence, code=code)),
        ),
        (
            "training_month",
            lambda: sum(1 for _ in partition.iter_training_rows(start, end, snapshot_sequence=sequence)),
        ),
        ("shadow_month", lambda: sum(1 for _ in partition.iter_shadow_facts(start, end, snapshot_sequence=sequence))),
        ("missing_dates", lambda: partition.missing_price_dates(sequence)),
        ("missing_counts", lambda: partition.missing_price_counts(sequence, (code,))),
        ("count_code", lambda: partition.count_range(start, end, snapshot_sequence=sequence, codes=(code,))),
        ("observations_after", lambda: partition.has_observations_after(sequence)),
        ("changed_revisions", lambda: partition.changed_revisions(sample[:8])),
        ("resume_keys", lambda: partition.read_price_keys(((code, sample[-1].trade_date),), sequence)),
    )
    queries: list[_Query] = []
    for name, operation in operations:
        statements.clear()
        operation()
        queries.extend(
            _Query(f"{name}:{number}", sql)
            for number, sql in enumerate(dict.fromkeys(statements))
            if sql.lstrip().upper().startswith(("SELECT", "WITH")) and "FROM metadata" not in sql
        )
    return tuple(queries), sample, sequence


def _query_digest(path: Path, query: _Query) -> str:
    digest = hashlib.sha256()
    with SqliteSession(path, _READ) as connection, _bounded(connection) as check:
        for row in connection.run(query.sql):
            check()
            digest.update(json.dumps(tuple(row), ensure_ascii=True, separators=(",", ":")).encode())
            digest.update(b"\n")
    return digest.hexdigest()


def _read(path: Path, query: _Query) -> _ReadMeasurement:
    steps = 0
    deadline = time.monotonic() + 60

    def progress() -> int:
        nonlocal steps
        steps += 1000
        return int(time.monotonic() >= deadline)

    with SqliteSession(path, _READ) as connection:
        connection.observe_progress(progress, 1000)
        started = time.perf_counter()
        count = 0
        for _row in connection.run(query.sql):
            count += 1
            if time.monotonic() >= deadline:
                raise TimeoutError("history_index_query_timeout")
        elapsed = (time.perf_counter() - started) * 1000
    return _ReadMeasurement(elapsed, count, steps)


def _plan(path: Path, query: _Query) -> tuple[str, ...]:
    with SqliteSession(path, _READ) as connection, _bounded(connection):
        return tuple(str(row[3]) for row in connection.run("EXPLAIN QUERY PLAN " + query.sql))


def _change_index(path: Path, sql: str) -> float:
    started = time.perf_counter()
    with SqliteSession(path, _WRITE) as connection, _bounded(connection), SqliteTransaction(connection, immediate=True):
        connection.run(sql)
    return (time.perf_counter() - started) * 1000


def _write(path: Path, sample: tuple[HistoryRevision, ...], ddl: str, *, deferred: bool) -> _WriteMeasurement:
    first = sample[0]
    partition = HistoryPricePartition(path, first.trade_date.year, first.trade_date.month)
    partition.initialize()
    if deferred:
        _change_index(path, "DROP INDEX " + _CANDIDATE_INDEX)
    started = time.perf_counter()
    partition.save_revision_batches(sample)
    write_ms = (time.perf_counter() - started) * 1000
    rebuild_ms = _change_index(path, ddl) if deferred else 0.0
    proof = partition.seal()
    return _WriteMeasurement(
        write_ms, rebuild_ms, write_ms + rebuild_ms, path.stat().st_size, proof.logical_hash, proof.row_count
    )


def EvaluateHistoryIndexes(
    root: Path, directory: Path, *, rounds: int = 5, sample_count: int = 5000
) -> dict[str, object]:
    """Source is read-only; all DDL and sample writes are confined to a new external directory."""
    if not 1 <= rounds <= 9 or not 1 <= sample_count <= 5000:
        raise ValueError("history_index_probe_bounds_invalid")
    source = _select_source(root)
    target = directory.resolve()
    protected = (root.resolve(), PROJECT_ROOT.resolve())
    if (
        not directory.is_absolute()
        or directory.is_symlink()
        or any(target == path or target.is_relative_to(path) or path.is_relative_to(target) for path in protected)
    ):
        raise ValueError("history_index_probe_directory_invalid")
    if shutil.disk_usage(target.parent).free < source.stat().st_size * 4 + 256 * 1024**2:
        raise ValueError("history_index_probe_disk_space_insufficient")
    target.mkdir(exist_ok=False)
    baseline, candidate = target / "baseline.sqlite3", target / "candidate.sqlite3"
    _copy(source, baseline)
    indexes, ddl, year, records = _inventory(baseline)
    queries, sample, sequence = _queries(baseline, year, sample_count)
    _copy(baseline, candidate)
    _change_index(candidate, "DROP INDEX " + _CANDIDATE_INDEX)
    schema_rejected = False
    try:
        HistoryPricePartition(candidate, year, sample[0].trade_date.month).seal(sequence)
    except HistoryPricePartitionError as exc:
        if str(exc) != "history month index contract is invalid":
            raise
        schema_rejected = True
    comparisons: list[dict[str, object]] = []
    for query in queries:
        baseline_plan, candidate_plan = _plan(baseline, query), _plan(candidate, query)
        equal = _query_digest(baseline, query) == _query_digest(candidate, query)
        runs: dict[str, list[_ReadMeasurement]] = {"baseline": [], "candidate": []}
        for number in range(rounds):
            order = ("baseline", "candidate") if number % 2 == 0 else ("candidate", "baseline")
            for label in order:
                runs[label].append(_read(baseline if label == "baseline" else candidate, query))
        before = statistics.median(run.milliseconds for run in runs["baseline"])
        after = statistics.median(run.milliseconds for run in runs["candidate"])
        comparisons.append(
            dict(
                workload=query.name,
                equal=equal,
                baseline_plan=baseline_plan,
                candidate_plan=candidate_plan,
                baseline_median_ms=before,
                candidate_median_ms=after,
                ratio=after / before,
                runs={label: [asdict(run) for run in values] for label, values in runs.items()},
            )
        )
    rebuild_ms = _change_index(candidate, ddl)
    proof_before = HistoryPricePartition(baseline, year, sample[0].trade_date.month).seal(sequence)
    proof_after = HistoryPricePartition(candidate, year, sample[0].trade_date.month).seal(sequence)
    writes: dict[str, list[_WriteMeasurement]] = {"eager": [], "deferred": []}
    for number in range(rounds):
        order = ("eager", "deferred") if number % 2 == 0 else ("deferred", "eager")
        for label in order:
            writes[label].append(
                _write(target / f"{label}-{number}.sqlite3", sample, ddl, deferred=label == "deferred")
            )
    equal = (
        all(item["equal"] for item in comparisons)
        and proof_before == proof_after
        and len({(run.logical_hash, run.row_count) for values in writes.values() for run in values}) == 1
    )
    return {
        "source": source.name,
        "source_year": year,
        "source_records": records,
        "source_bytes": baseline.stat().st_size,
        "source_publication_verified": False,
        "rounds": rounds,
        "sample_rows": len(sample),
        "indexes": indexes,
        "candidate_index": _CANDIDATE_INDEX,
        "schema_rejects_missing_index": schema_rejected,
        "queries": comparisons,
        "annual_index_rebuild_ms": rebuild_ms,
        "writes": {label: [asdict(run) for run in values] for label, values in writes.items()},
        "equal": equal,
        "production_change_applied": False,
        "cache_policy": "alternating order, new connection; OS cache uncontrolled; same process",
        "scope": "one annual file and latest populated month; SQL replay excludes decoding/startup orchestration",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history-root", type=Path, required=True)
    parser.add_argument("--query-rounds", type=int, default=5)
    parser.add_argument("--write-sample-count", type=int, default=5000)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="trader-history-indexes-") as scratch:
        report = EvaluateHistoryIndexes(
            args.history_root,
            Path(scratch) / "evaluation",
            rounds=args.query_rounds,
            sample_count=args.write_sample_count,
        )
    emit_report(
        {
            "schema_version": "history-index-evaluation-v1",
            "status": "passed" if report["equal"] else "failed",
            "summary": report,
        }
    )
    return 0 if report["equal"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
