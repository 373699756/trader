"""Offline joint/single sync and startup-input baselines on disposable databases."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import median
from tempfile import TemporaryDirectory
from typing import TypeVar, cast
from zoneinfo import ZoneInfo

from trader.download.application.read_published_history import ReadPublishedHistoryUseCase
from trader.download.application.sync_data import SyncData, SyncTarget
from trader.download.application.update_qfq import UpdateQfqWindows
from trader.download.domain.baostock_daily import BaoStockSecurity
from trader.download.domain.company_evidence import CompanyEvidenceSyncResult, Disclosure, DisclosurePage
from trader.download.domain.history_maintenance import HistoryMaintenanceStatus
from trader.download.domain.history_sync import HistorySupplierContext
from trader.download.domain.prepared_reference import PreparedReference
from trader.download.domain.published_history import PublishedHistoryWindow
from trader.download.domain.qfq_window import QfqUpdateResult
from trader.download.infra.company_evidence_index import SQLiteCompanyEvidenceIndex, SQLiteCompanyEvidenceReader
from trader.download.infra.download_settings import LoadHistorySyncConfiguration
from trader.download.infra.history_sync_runner import RunHistorySync
from trader.download.infra.published_history_snapshot import SQLitePublishedHistorySnapshot
from trader.download.infra.qfq_sqlite import SQLiteQfqWindowCache
from trader.download.infra.qfq_update_runner import QfqUpdateRunner
from trader.download.infra.reference_publication import ReferencePublication
from trader.infra.serialization.canonical_codec import CanonicalDigest
from trader.infra.settings.data_paths import DataPaths
from trader.infra.storage_types import StorageKind
from trader.recommendation.infra.market_data.published_company_research import PublishedCompanyResearch
from trader.recommendation.infra.market_data.published_history_cache import PublishedHistoryCache
from trader.recommendation.infra.market_data.published_references import PublishedReferences
from trader.recommendation.infra.market_data.shared_history_projection import SharedHistoryProjection

from .database_measurement import DatabaseMeasurement
from .diagnostic_types import PROJECT_ROOT
from .history_write_benchmark import HistoryFixtureSupplier, MeasureHistoryWrites
from .reporting import emit_report

_Result = TypeVar("_Result")
_NOW = datetime(2026, 10, 16, 20, 30, tzinfo=ZoneInfo("Asia/Shanghai"))


@dataclass(frozen=True)
class _InputIdentities:
    reference: str | None
    history: str
    company: str
    market: str | None


@dataclass(frozen=True)
class _Startup:
    identities: _InputIdentities
    observations: dict[str, object]


class _Supplier(HistoryFixtureSupplier):
    def load_qfq_context(self, as_of: date, sessions: int) -> HistorySupplierContext:
        return self.load_context(as_of, sessions)

    def fetch_window(self, security: BaoStockSecurity, dates: tuple[date, ...]) -> PublishedHistoryWindow:
        return self.fetch_tencent_window(security, dates)


class _SyncFixture:
    """Real sync coordinator and persistence adapters with finite synthetic supplier facts."""

    def __init__(self, root: Path, size: int, sessions: int, storage_kind: StorageKind) -> None:
        self.paths = DataPaths(root)
        self.root = root
        self.sessions = sessions
        self.storage_kind = storage_kind
        days = tuple(_NOW.date() - timedelta(days=sessions - offset - 1) for offset in range(sessions))
        self.supplier = _Supplier(days, size)
        context = self.supplier.load_context(_NOW.date(), sessions)
        self.reference = PreparedReference(
            context.universe,
            context.st_evidence,
            _NOW,
            days + tuple(_NOW.date() + timedelta(days=offset) for offset in range(1, 10)),
        )
        self.preparations = 0
        self.phases: dict[str, dict[str, object]] = {}

    def _measure(self, name: str, operation: Callable[[], _Result]) -> _Result:
        with DatabaseMeasurement(self.root) as measurement:
            result = operation()
        self.phases[name] = measurement.result
        return result

    def prepare(self) -> PreparedReference:
        def publish() -> PreparedReference:
            self.preparations += 1
            ReferencePublication(self.paths.reference).publish(self.reference)
            return self.reference

        return self._measure("prepare", publish)

    def history(self, reference: PreparedReference) -> HistoryMaintenanceStatus:
        if reference is not self.reference:
            raise RuntimeError("database_baseline_reference_not_shared")
        configuration = replace(
            LoadHistorySyncConfiguration(PROJECT_ROOT, self.paths),
            sessions=self.sessions,
            reread_sessions=min(5, self.sessions),
            minimum_free_bytes=0,
        )
        return self._measure(
            "history",
            lambda: RunHistorySync(configuration, self.supplier, clock=lambda: _NOW, storage_kind=self.storage_kind),
        )

    def market(self, reference: PreparedReference) -> QfqUpdateResult:
        if reference is not self.reference:
            raise RuntimeError("database_baseline_reference_not_shared")
        v2 = SQLiteQfqWindowCache(self.paths.daily_window, "v2")
        v3 = SQLiteQfqWindowCache(self.paths.daily_window, "v3")
        updater = UpdateQfqWindows(
            v2,
            v3,
            self.supplier,
            cancel_requested=lambda: False,
            report=lambda _progress: None,
        )
        runner = QfqUpdateRunner(
            ReadPublishedHistoryUseCase(SQLitePublishedHistorySnapshot(self.paths.history)),
            updater,
            v2,
            v3,
            self.paths.daily_window.with_suffix(".lock"),
            lambda: _NOW,
        )
        return self._measure("market", runner.execute)

    def company(self, reference: PreparedReference) -> CompanyEvidenceSyncResult:
        if reference is not self.reference:
            raise RuntimeError("database_baseline_reference_not_shared")

        def publish() -> CompanyEvidenceSyncResult:
            started = time.monotonic()
            index = SQLiteCompanyEvidenceIndex(self.paths.company)
            self._measure("company_initialize", index.initialize)
            partition = index.partitions(("sse",), _NOW)[0]
            codes = tuple(item.code for item in reference.eligible_universe)
            rows = tuple(
                Disclosure("fixture", f"{code}:{offset}", (code,), "普通公告", _NOW, "", str(offset))
                for code in codes
                for offset in range(3)
            )
            changed = self._measure(
                "company_page", lambda: index.commit_page(partition, DisclosurePage(rows, len(rows), False), _NOW)
            )
            published = self._measure("company_publish", lambda: index.publish(codes, _NOW))
            return CompanyEvidenceSyncResult(
                _NOW, len(codes), published, 1, changed, 0, 0, (), time.monotonic() - started
            )

        # Page and publication measurements are separate scopes, not nested patches.
        return publish()

    def execute(self, target: SyncTarget) -> dict[str, object]:
        self.phases.clear()
        before = self.preparations
        started = time.monotonic()
        result = SyncData(self.prepare, self.history, self.market, self.company, lambda: False).execute(target)
        expected = ("history", "market", "company") if target == "all" else (target,)
        if result.exit_code or tuple(item.task for item in result.tasks) != expected or self.preparations - before != 1:
            raise RuntimeError(f"database_baseline_sync_failed:{target}")
        return {
            "target": target,
            "wall_seconds": time.monotonic() - started,
            "prepare_calls": self.preparations - before,
            "tasks": [item.task for item in result.tasks],
            "phases": dict(self.phases),
        }

    def startup(self) -> _Startup:
        publication = ReferencePublication(self.paths.reference)
        references = PublishedReferences(publication.read, publication.identity, lambda _now: (), max_age_seconds=86400)
        self._measure("startup_reference", references.initialize)
        history = ReadPublishedHistoryUseCase(SQLitePublishedHistorySnapshot(self.paths.history))
        projection = SharedHistoryProjection(history, monotonic=time.monotonic)
        caches = tuple(
            PublishedHistoryCache(
                history,
                lookback_sessions=sessions,
                monotonic=time.monotonic,
                open_dates=lambda: self.reference.open_dates,
                learned_rank_inputs=rank,
                sequence_inputs=sequence,
                projection_owner=projection,
            )
            for sessions, rank, sequence in (
                (251, True, False),
                (251, False, False),
                (61, False, False),
                (250, False, True),
            )
        )
        self._measure("startup_history", lambda: caches[0].initialize(cancelled=lambda: False))
        codes = tuple(item.code for item in self.reference.eligible_universe)
        research = PublishedCompanyResearch(
            SQLiteCompanyEvidenceReader(self.paths.company),
            wall_clock=lambda: _NOW,
            open_dates=lambda: self.reference.open_dates,
            monotonic=time.monotonic,
        )
        self._measure("startup_company", lambda: research.initialize(codes, cancelled=lambda: False))

        def memory_reads() -> None:
            for _ in range(3):
                references.versions()
                research.cached(codes, include_structured=True)
                for cache in caches:
                    cache.entries()

        self._measure("loaded_memory_reads", memory_reads)
        hot = self.phases["loaded_memory_reads"]
        if hot["connections"] != 0 or hot["statements"] != 0:
            raise RuntimeError("database_baseline_preload_reread")
        identities = tuple(cache.loaded_identity for cache in caches)
        history_present = (self.paths.history / "catalog.sqlite3").exists()
        if history_present and (projection.scan_count != 1 or len(set(identities)) != 1 or not caches[0].entries()):
            raise RuntimeError("database_baseline_history_projection_failed")
        if research.status().entries != (len(codes) if self.paths.company.exists() else 0):
            raise RuntimeError("database_baseline_company_projection_failed")
        published = _InputIdentities(
            references.status().loaded_identity, identities[0], research.loaded_identity, self.window_identity()
        )
        return _Startup(
            published,
            {
                "phases": {
                    name: value
                    for name, value in self.phases.items()
                    if name.startswith("startup_") or name == "loaded_memory_reads"
                },
                "history_scan_count": projection.scan_count,
                "history_window_count": projection.window_count,
                "profile_entries": [len(cache.entries()) for cache in caches],
                "history_available": history_present,
                "company_entries": research.status().entries,
                "identities": {
                    "reference": published.reference,
                    "history": published.history,
                    "company": published.company,
                    "market": published.market,
                },
            },
        )

    def window_identity(self) -> str | None:
        if not self.paths.daily_window.exists():
            return None
        codes = tuple(item.code for item in self.reference.eligible_universe)
        return CanonicalDigest(SQLiteQfqWindowCache(self.paths.daily_window, "v2").read_codes(codes))


def MeasureSyncBaseline(root: Path, *, size: int, sessions: int, storage_kind: StorageKind) -> dict[str, object]:
    targets: tuple[SyncTarget, ...] = ("all", "history", "market", "company")
    runs: dict[str, dict[str, object]] = {}
    identities: dict[str, _InputIdentities] = {}
    for target in targets:
        fixture = _SyncFixture(root / target, size, sessions, storage_kind)
        sync = fixture.execute(target)
        startup = fixture.startup()
        identities[target] = startup.identities
        # Each independent target starts with absent unrelated directories.
        expected = ("history", "market", "company") if target == "all" else (target,)
        actual = tuple(
            name
            for name, path in (
                ("history", fixture.paths.history),
                ("market", fixture.paths.daily_window),
                ("company", fixture.paths.company),
            )
            if path.exists()
        )
        if actual != expected:
            raise RuntimeError("database_baseline_independent_target_wrote_other_data")
        repeat = fixture.execute(target)
        restarted = fixture.startup()
        if restarted.identities != startup.identities:
            raise RuntimeError("database_baseline_repeat_changed_publication")
        runs[target] = {
            "initial": sync,
            "startup": startup.observations,
            "same_day": repeat,
            "restarted": restarted.observations,
        }
    combined = identities["all"]
    if (
        identities["history"].history != combined.history
        or identities["market"].market != combined.market
        or identities["company"].company != combined.company
        or any(value.reference != combined.reference for value in identities.values())
    ):
        raise RuntimeError("database_baseline_joint_single_content_mismatch")
    return {"targets": runs, "joint_single_equivalent": True, "independent_targets": True}


def _worker(size: int, sessions: int, storage_kind: StorageKind) -> dict[str, object]:
    with TemporaryDirectory(prefix="trader-database-baseline-") as scratch:
        sync = MeasureSyncBaseline(Path(scratch), size=size, sessions=sessions, storage_kind=storage_kind)
        history = MeasureHistoryWrites(size, sessions, storage_kind=storage_kind)
    if history["status"] != "passed":
        raise RuntimeError("database_baseline_history_cases_failed")
    return {"sync": sync, "history": history["summary"]}


def _numeric_metrics(value: object, prefix: str = "") -> dict[str, float]:
    """Flatten only diagnostic numeric measurements at the JSON report boundary."""
    metrics: dict[str, float] = {}
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise ValueError("database_baseline_invalid_report_key")
            path = f"{prefix}.{key}" if prefix else key
            if isinstance(child, (int, float)) and not isinstance(child, bool):
                if key.endswith(("_seconds", "_bytes")) or key in {
                    "seconds",
                    "connections",
                    "statements",
                    "transactions",
                    "commits",
                }:
                    metrics[path] = child
            else:
                metrics.update(_numeric_metrics(child, path))
    elif isinstance(value, list):
        for position, child in enumerate(value):
            label = child.get("case", str(position)) if isinstance(child, dict) else str(position)
            metrics.update(_numeric_metrics(child, f"{prefix}.{label}"))
    return metrics


def _median_metrics(runs: list[dict[str, object]]) -> dict[str, float]:
    measurements = [_numeric_metrics(run) for run in runs]
    common = set.intersection(*(set(run) for run in measurements))
    return {key: median(run[key] for run in measurements) for key in sorted(common)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--size", type=int, default=8)
    parser.add_argument("--sessions", type=int, default=2000)
    parser.add_argument("--rounds", type=int, choices=range(1, 6), default=5)
    parser.add_argument("--storage-kind", choices=("ssd", "hdd", "unknown"), default="unknown")
    parser.add_argument("--worker", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.size <= 32 or not 5 <= args.sessions <= 2000:
        parser.error("database baseline requires 1..32 stocks and 5..2000 sessions")
    if args.worker:
        emit_report(_worker(args.size, args.sessions, args.storage_kind))
        return 0
    runs: list[dict[str, object]] = []
    for _ in range(args.rounds):
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "scripts.runtime_diagnostics.database_baseline",
                "--worker",
                "--size",
                str(args.size),
                "--sessions",
                str(args.sessions),
                "--storage-kind",
                args.storage_kind,
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=600,
        )
        decoded = json.loads(completed.stdout)
        if not isinstance(decoded, dict) or set(decoded) != {"sync", "history"}:
            raise ValueError("database_baseline_invalid_worker_report")
        runs.append(cast(dict[str, object], decoded))
    emit_report(
        {
            "schema_version": "database-baseline-v1",
            "status": "passed",
            "summary": {
                "fixture": True,
                "rounds": args.rounds,
                "stocks": args.size,
                "sessions": args.sessions,
                "physical_http_requests": 0,
                "storage_kind": args.storage_kind,
                "runs": runs,
                "median_metrics": _median_metrics(runs),
                "cache_policy": "fresh worker and databases per round; operating-system cache is uncontrolled",
                "measurement_policy": "scoped public SQLite wrapper instrumentation; statements count submitted run/run_many rows; successful BEGIN/COMMIT counts; WAL sampled every 50 ms; SQL time includes commit and fetch, excludes connection setup/PRAGMAs; RSS is process peak",
                "scope": "SyncData orchestration, real persistence and startup input projections; synthetic prices and one company announcement page; no full service, models, PDF or supplier acceptance",
            },
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
