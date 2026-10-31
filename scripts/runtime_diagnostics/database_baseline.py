"""Offline joint/single sync and startup-input baselines on disposable databases."""

from __future__ import annotations

from trader.infra.sqlite_policy import SqliteIoPolicy

import argparse
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import median
from tempfile import TemporaryDirectory
from typing import Literal, TypedDict, TypeVar, cast
from zoneinfo import ZoneInfo

import trader
from trader.download.application.read_published_history import ReadPublishedHistoryUseCase
from trader.download.application.sync_data import SyncData, SyncTarget
from trader.download.application.update_qfq import UpdateQfqWindows
from trader.download.domain.baostock_daily import BaoStockSecurity
from trader.download.domain.company_evidence import CompanyEvidenceSyncResult, CompanySyncConfiguration, Disclosure, DisclosurePage
from trader.download.domain.history_maintenance import HistoryMaintenanceStatus
from trader.download.domain.history_sync import HistorySupplierContext
from trader.download.domain.prepared_reference import PreparedReference
from trader.download.domain.published_history import PublishedHistoryWindow
from trader.download.domain.qfq_window import QfqUpdateResult
from trader.download.infra.company_document_files import CompanyDocumentFiles
from trader.download.infra.company_evidence_index import SQLiteCompanyEvidenceIndex
from trader.download.infra.company_facts_reader import SQLiteCompanyEvidenceReader
from trader.download.infra.download_settings import (
    LoadCompanySyncConfiguration,
    LoadHistorySyncConfiguration,
    LoadQfqWindowConfiguration,
)
from trader.download.infra.history_sync_runner import RunHistorySync
from trader.download.infra.published_history_snapshot import SQLitePublishedHistorySnapshot
from trader.download.infra.qfq_sqlite import SQLiteQfqWindowCache
from trader.download.infra.qfq_update_runner import QfqUpdateRunner
from trader.download.infra.reference_index import SQLiteReferenceIndex
from trader.infra.serialization.canonical_codec import CanonicalDigest
from trader.infra.settings.data_paths import DataPaths
from trader.infra.settings import LoadRuntimeSettings, LoadStrategySettings
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


def _company_documents(path: Path, configuration: CompanySyncConfiguration) -> CompanyDocumentFiles:
    return CompanyDocumentFiles(
        path,
        max_text_characters=configuration.extended_document_limits.max_text_characters,
        max_bytes=configuration.temporary_text_capacity_bytes,
        reserve_bytes=configuration.temporary_disk_reserve_bytes,
    )


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


class _MetricComparison(TypedDict):
    metric: str
    baseline_median: float
    candidate_median: float
    ratio: float | None
    improved_rounds: int


class _Supplier(HistoryFixtureSupplier):
    def load_qfq_context(self, as_of: date, sessions: int) -> HistorySupplierContext:
        return self.load_context(as_of, sessions)

    def fetch_window(self, security: BaoStockSecurity, dates: tuple[date, ...]) -> PublishedHistoryWindow:
        return self.fetch_tencent_window(security, dates)


class _SyncFixture:
    """Real sync coordinator and persistence adapters with finite synthetic supplier facts."""

    def __init__(self, root: Path, size: int, sessions: int, storage_kind: StorageKind, *, sqlite_policy: SqliteIoPolicy) -> None:
        self._sqlite_policy = sqlite_policy
        self.preload_settings = LoadRuntimeSettings(PROJECT_ROOT / "config/runtime.json").pipeline.history_preload
        self.company_configuration = LoadCompanySyncConfiguration(PROJECT_ROOT)
        self.company_policy = LoadStrategySettings(PROJECT_ROOT / "config/strategy.json").company_scoring
        self.paths = DataPaths(root)
        self.root = root
        self.sessions = sessions
        self.storage_kind = storage_kind
        days = tuple(_NOW.date() - timedelta(days=sessions - offset - 1) for offset in range(sessions))
        self.supplier = _Supplier(days, size, sqlite_policy=sqlite_policy)
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
            SQLiteReferenceIndex(self.paths.reference_database, sqlite_policy=self._sqlite_policy).publish(self.reference, self.reference.st_evidence)
            self.supplier.reference_id = self.reference.identity
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
            lambda: RunHistorySync(configuration, self.supplier, clock=lambda: _NOW, storage_kind=self.storage_kind, sqlite_policy=self._sqlite_policy),
        )

    def market(self, reference: PreparedReference) -> QfqUpdateResult:
        if reference is not self.reference:
            raise RuntimeError("database_baseline_reference_not_shared")
        v2 = SQLiteQfqWindowCache(self.paths.daily_window, "v2", sqlite_policy=self._sqlite_policy)
        v3 = SQLiteQfqWindowCache(self.paths.daily_window, "v3", sqlite_policy=self._sqlite_policy)
        updater = UpdateQfqWindows(
            v2,
            v3,
            self.supplier,
            cancel_requested=lambda: False,
            report=lambda _progress: None,
            configuration=LoadQfqWindowConfiguration(PROJECT_ROOT),
        )
        runner = QfqUpdateRunner(
            ReadPublishedHistoryUseCase(SQLitePublishedHistorySnapshot(self.paths.history, sqlite_policy=self._sqlite_policy)),
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
            index = SQLiteCompanyEvidenceIndex(
                self.paths.company_collection,
                facts_path=self.paths.company,
                documents=_company_documents(self.paths.company_documents, self.company_configuration),
                event_policy=self.company_policy.events, sqlite_policy=self._sqlite_policy,
            )
            index.initialize()
            codes = tuple(item.code for item in reference.eligible_universe)
            rows = tuple(
                Disclosure("fixture", f"{code}:{offset}", (code,), "普通公告", _NOW, "", str(offset))
                for code in codes
                for offset in range(3)
            )
            partition = index.partitions(("sse",), _NOW)[0]
            changed = 0
            for offset in range(0, len(rows), 30):
                page = DisclosurePage(rows[offset : offset + 30], len(rows), offset + 30 < len(rows))
                changed += index.commit_page(partition, page, _NOW)
                partition = replace(
                    partition,
                    page=partition.page + 1,
                    expected_rows=len(rows),
                    received_rows=partition.received_rows + len(page.rows),
                )
            published = index.publish(codes, _NOW)
            return CompanyEvidenceSyncResult(
                _NOW, len(codes), published, (len(rows) + 29) // 30, changed, 0, 0, (), time.monotonic() - started
            )

        # Include initialization, all page commits and close/checkpoint work in one scope.
        return self._measure("company", publish)

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
            "files_bytes": sum(
                path.stat().st_size
                for path in self.root.rglob("*")
                if path.is_file() and (path.suffix == ".sqlite3" or path.name.endswith(("-wal", "-shm")))
            ),
            "prepare_calls": self.preparations - before,
            "tasks": [item.task for item in result.tasks],
            "phases": dict(self.phases),
        }

    def startup(self) -> _Startup:
        publication = SQLiteReferenceIndex(self.paths.reference_database, sqlite_policy=self._sqlite_policy)
        references = PublishedReferences(publication.read, publication.identity, lambda _now: (), max_age_seconds=86400)
        self._measure("startup_reference", references.initialize)
        history = ReadPublishedHistoryUseCase(SQLitePublishedHistorySnapshot(self.paths.history, sqlite_policy=self._sqlite_policy))
        projection = SharedHistoryProjection(history, monotonic=time.monotonic, settings=self.preload_settings)
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
            SQLiteCompanyEvidenceReader(self.paths.company, event_policy=self.company_policy.events, sqlite_policy=self._sqlite_policy),
            wall_clock=lambda: _NOW,
            open_dates=lambda: self.reference.open_dates,
            monotonic=time.monotonic,
            company_policy=self.company_policy,
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
        return cast(str, CanonicalDigest(SQLiteQfqWindowCache(self.paths.daily_window, "v2", sqlite_policy=self._sqlite_policy).read_codes(codes)))


def MeasureSyncBaseline(root: Path, *, size: int, sessions: int, storage_kind: StorageKind, sqlite_policy: SqliteIoPolicy) -> dict[str, object]:
    targets: tuple[SyncTarget, ...] = ("all", "history", "market", "company")
    runs: dict[str, dict[str, object]] = {}
    identities: dict[str, _InputIdentities] = {}
    for target in targets:
        fixture = _SyncFixture(root / target, size, sessions, storage_kind, sqlite_policy=sqlite_policy)
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


def _worker(size: int, sessions: int, storage_kind: StorageKind, *, sqlite_policy: SqliteIoPolicy) -> dict[str, object]:
    with TemporaryDirectory(prefix="trader-database-baseline-") as scratch:
        sync = MeasureSyncBaseline(Path(scratch), size=size, sessions=sessions, storage_kind=storage_kind, sqlite_policy=sqlite_policy)
        history = MeasureHistoryWrites(size, sessions, storage_kind=storage_kind, sqlite_policy=sqlite_policy)
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


def _run_worker(checkout: Path, size: int, sessions: int, storage_kind: StorageKind) -> dict[str, object]:
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.runtime_diagnostics.database_baseline",
            "--worker",
            "--size",
            str(size),
            "--sessions",
            str(sessions),
            "--storage-kind",
            storage_kind,
            "--expected-product-root",
            str(checkout),
        ],
        cwd=checkout,
        env={**os.environ, "PYTHONPATH": str(checkout / "src"), "PYTHONHASHSEED": "0"},
        capture_output=True,
        text=True,
        check=True,
        timeout=600,
    )
    decoded = json.loads(completed.stdout)
    if not isinstance(decoded, dict) or set(decoded) != {"sync", "history"}:
        raise ValueError("database_baseline_invalid_worker_report")
    return cast(dict[str, object], decoded)


def _publication_facts(value: object, prefix: str = "") -> dict[str, object]:
    facts: dict[str, object] = {}
    if isinstance(value, dict):
        for key, child in value.items():
            path = f"{prefix}.{key}"
            if key in {"identities", "publication_hash", "rows", "gaps", "state", "requests", "requested_rows"}:
                facts[path] = child
            else:
                facts.update(_publication_facts(child, path))
    elif isinstance(value, list):
        for position, child in enumerate(value):
            facts.update(_publication_facts(child, f"{prefix}.{position}"))
    return facts


def _compare(
    baseline: Path,
    size: int,
    sessions: int,
    rounds: int,
    storage_kind: StorageKind,
    focus: Literal["history", "market-company"] = "history",
) -> dict[str, object]:
    baseline = baseline.resolve(strict=True)
    if baseline == PROJECT_ROOT or baseline.is_relative_to(PROJECT_ROOT) or not (baseline / "src/trader").is_dir():
        raise ValueError("database comparison requires an external product checkout")
    runs: dict[str, list[dict[str, object]]] = {"baseline": [], "candidate": []}
    for number in range(rounds):
        order = ("baseline", "candidate") if number % 2 == 0 else ("candidate", "baseline")
        for variant in order:
            checkout = baseline if variant == "baseline" else PROJECT_ROOT
            runs[variant].append(_run_worker(checkout, size, sessions, storage_kind))
        if _publication_facts(runs["baseline"][-1]) != _publication_facts(runs["candidate"][-1]):
            raise RuntimeError("database_comparison_publication_changed")
    old, new = _median_metrics(runs["baseline"]), _median_metrics(runs["candidate"])
    comparisons: list[_MetricComparison] = []
    before_metrics = [_numeric_metrics(run) for run in runs["baseline"]]
    after_metrics = [_numeric_metrics(run) for run in runs["candidate"]]
    for key in sorted(old.keys() & new.keys()):
        previous = [run[key] for run in before_metrics]
        current = [run[key] for run in after_metrics]
        comparisons.append(
            {
                "metric": key,
                "baseline_median": old[key],
                "candidate_median": new[key],
                "ratio": new[key] / old[key] if old[key] else (1.0 if new[key] == 0 else None),
                "improved_rounds": sum(after < before for before, after in zip(previous, current, strict=True)),
            }
        )
    history_walls = tuple(
        f"history.cases.{case}.database.wall_seconds"
        for case in ("initial", "next_day", "two_weeks", "same_day", "initial_resume")
    )
    primary_groups = (
        (history_walls,)
        if focus == "history"
        else tuple(
            tuple(f"sync.targets.{target}.{phase}.wall_seconds" for phase in ("initial", "same_day"))
            for target in ("market", "company")
        )
    )
    improved = [
        row["metric"]
        for row in comparisons
        if any(row["metric"] in group for group in primary_groups)
        and row["ratio"] is not None
        and row["ratio"] <= 0.9
        and row["improved_rounds"] >= 4
    ]
    regression_keys = (
        history_walls
        + tuple(
            f"sync.targets.{target}.{phase}.wall_seconds"
            for target in ("all", "history", "market", "company")
            for phase in ("initial", "same_day")
        )
        + tuple(f"sync.targets.{target}.startup.phases.startup_history.wall_seconds" for target in ("all", "history"))
        + tuple(f"sync.targets.{target}.startup.phases.startup_company.wall_seconds" for target in ("all", "company"))
    )
    regressions = [key for key in regression_keys if new[key] > old[key] * 1.05]
    space_ok = all(new[key] <= old[key] * 1.05 for key in old if key.endswith(".files_bytes"))
    rss_ok = (
        max(
            value
            for metrics in after_metrics
            for key, value in metrics.items()
            if key.endswith("process_peak_rss_bytes")
        )
        <= 2 * 1024**3
    )
    return {
        "runs": runs,
        "comparison": comparisons,
        "publication_equal": True,
        "acceptance": {
            "accepted": rounds >= 5
            and all(any(key in improved for key in group) for group in primary_groups)
            and not regressions
            and rss_ok,
            "focus": focus,
            "improved_cases": improved,
            "regressions": regressions,
            "database_size_within_5_percent": space_ok,
            "process_rss_within_2_gib": rss_ok,
            "policy": "5 alternating pairs; each primary group has a wall median improvement of at least 10% with 4/5 wins; all wall medians regress at most 5%; file size is observational only; RSS <= 2 GiB",
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--size", type=int, default=8)
    parser.add_argument("--sessions", type=int, default=2000)
    parser.add_argument("--rounds", type=int, choices=range(1, 6), default=5)
    parser.add_argument("--storage-kind", choices=("ssd", "hdd", "unknown"), default="unknown")
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--baseline-checkout", type=Path)
    parser.add_argument("--acceptance-focus", choices=("history", "market-company"), default="history")
    parser.add_argument("--expected-product-root", type=Path, default=PROJECT_ROOT)
    args = parser.parse_args()
    from trader.bootstrap import BuildSqlitePolicy
    from trader.infra.settings import LoadRuntimeSettings
    sqlite_policy = BuildSqlitePolicy(LoadRuntimeSettings(Path(__file__).resolve().parents[2] / "config/runtime.json"))
    if not 1 <= args.size <= 32 or not 5 <= args.sessions <= 2000:
        parser.error("database baseline requires 1..32 stocks and 5..2000 sessions")
    if args.worker:
        if Path(trader.__file__).resolve().parents[2] != args.expected_product_root.resolve():
            raise RuntimeError("database_comparison_wrong_product_import")
        emit_report(_worker(args.size, args.sessions, args.storage_kind, sqlite_policy=sqlite_policy))
        return 0
    if args.baseline_checkout is not None:
        result = _compare(
            args.baseline_checkout, args.size, args.sessions, args.rounds, args.storage_kind, args.acceptance_focus
        )
        acceptance = cast(dict[str, object], result["acceptance"])
        status = "passed" if acceptance["accepted"] else "degraded"
    else:
        runs = [_run_worker(PROJECT_ROOT, args.size, args.sessions, args.storage_kind) for _ in range(args.rounds)]
        result = {"runs": runs, "median_metrics": _median_metrics(runs)}
        status = "passed"
    emit_report(
        {
            "schema_version": "database-baseline-v1",
            "status": status,
            "summary": {
                "fixture": True,
                "rounds": args.rounds,
                "stocks": args.size,
                "sessions": args.sessions,
                "physical_http_requests": 0,
                "storage_kind": args.storage_kind,
                **result,
                "cache_policy": "fresh worker and databases per round; operating-system cache is uncontrolled",
                "measurement_policy": "scoped public SQLite wrapper instrumentation; statements count submitted run/run_many rows; successful BEGIN/COMMIT counts; WAL sampled every 50 ms; SQL time includes commit and fetch, excludes connection setup/PRAGMAs; RSS is process peak",
                "scope": "SyncData orchestration, real persistence and startup input projections; synthetic prices and company announcements in 30-row pages; no full service, models, PDF or supplier acceptance",
            },
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
