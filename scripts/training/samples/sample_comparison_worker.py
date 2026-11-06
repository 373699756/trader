"""One isolated checkout's sample write/read measurements; never trains or publishes models."""

from __future__ import annotations

from scripts.core.paths import PROJECT_ROOT as TOOL_ROOT
from scripts.training.output_paths import RequireIndependentOutput

import argparse
import hashlib
import json
import resource
import platform
import re
import sqlite3
import sys
import time
from contextlib import ExitStack, closing, nullcontext
from datetime import date, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict, cast

if TYPE_CHECKING:
    from cProfile import Profile

    from trader.infra.sqlite_policy import SqliteIoPolicy
    from scripts.training.samples.computation_candidates import ComputationCandidate
    from trader.infra.disk_types import DiskKind
    from trader.training.application.resources import TrainingSampleSettings


class _SqliteArguments(TypedDict, total=False):
    sqlite_policy: SqliteIoPolicy


class TrainingSampleArguments(TypedDict, total=False):
    sample_settings: TrainingSampleSettings


def SampleArgumentsForCheckout(checkout: Path, *, medium: DiskKind) -> TrainingSampleArguments:
    """Select an explicitly frozen diagnostic API; never supply product defaults."""
    document = json.loads((checkout / "config/training.json").read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("sample_comparison_configuration_invalid")
    resources = document.get("resources")
    if resources is None:
        return {}  # Explicit pre-resource-schema historical checkout.
    machine = resources.get("ssd" if medium == "ssd" else "hdd") if isinstance(resources, dict) else None
    if not isinstance(machine, dict):
        raise ValueError("sample_comparison_resource_schema_invalid")
    if "samples" not in machine:
        return {}  # Explicit pre-sample-policy historical checkout; current APIs reject missing injection.
    from trader.infra.settings.training import LoadTrainingResourceSettings

    return {"sample_settings": LoadTrainingResourceSettings(checkout, medium).samples}


def _measure_feature_construction(
    checkout: Path,
    root: Path,
    size: int,
    sessions: int,
    *,
    medium: DiskKind,
    sqlite_policy: SqliteIoPolicy,
    computation_candidate: ComputationCandidate = "baseline",
    profile_functions: bool = False,
) -> dict[str, object]:
    from scripts.training.samples.computation_candidates import ProjectComputationCandidate
    from scripts.training.samples.computation_candidates import UseComputationCandidate

    with UseComputationCandidate(computation_candidate) as counts:
        report = _construct_samples(
            checkout,
            root,
            size,
            sessions,
            medium=medium,
            sqlite_policy=sqlite_policy,
            profile_functions=profile_functions,
        )
        report["computation_candidate"] = ProjectComputationCandidate(counts)
    return report


def _require_profile_available() -> None:
    if sys.getprofile() is not None:
        raise ValueError("sample_function_profile_already_running")
    if sys.version_info >= (3, 12):
        from sys import monitoring

        if monitoring.get_tool(monitoring.PROFILER_ID) is not None:
            raise ValueError("sample_function_profile_already_running")


def _construct_samples(
    checkout: Path,
    root: Path,
    size: int,
    sessions: int,
    *,
    medium: DiskKind,
    sqlite_policy: SqliteIoPolicy,
    profile_functions: bool = False,
) -> dict[str, object]:
    """Measure actual sample builders on one deterministic full-date fixture; no fitting."""
    RequireIndependentOutput(root, TOOL_ROOT)
    RequireIndependentOutput(root, checkout)
    if profile_functions:
        _require_profile_available()
    from collections.abc import Callable, Iterator
    from dataclasses import dataclass

    import trader
    from trader.download.domain.baostock_daily import BaoStockCalendar
    from trader.download.domain.history_revision import HistoryTrainingPoint, HistoryTrainingRow
    from trader.infra.sqlite_connection import SqliteConnectionOptions, SqliteSession
    from trader.training.application.tomorrow_training import TomorrowTrainingWindow
    from trader.training.domain.training_dates import BuildTrainingDateSplit
    from trader.training.domain.v1.learned_rank import BuildLearnedRankDateSplit
    from trader.training.infra.history.history_training_input import HistoryTrainingInputSnapshot
    from trader.training.infra.implementation_identity import TrainingImplementationIdentity
    from trader.training.infra.profile.v1.samples import BuildLearnedRankSamples, LearnedRankSampleIndex
    from trader.training.infra.profile.v1.settings import LoadV1TrainingSettings
    from trader.training.infra.profile.v2.contracts import V2_TRAINING_PROFILE
    from trader.training.infra.profile.v3.contracts import V3_TRAINING_PROFILE
    from trader.training.infra.profile.v5.contracts import V5_TRAINING_PROFILE
    from trader.training.infra.sample_builder import BuildTrainingSamples, TrainingSampleBuildRequest
    from trader.training.infra.sample_index import SQLiteTrainingSampleIndex
    from trader.training.infra.sample_sequences import SampleSequenceLayout
    from trader.training.infra.training_settings import LoadProfileTrainingSettings

    sample_arguments = SampleArgumentsForCheckout(checkout, medium=medium)

    if not 1 <= size <= 128 or not 1800 <= sessions <= 2000:
        raise ValueError("sample_construction_requires_1_to_128_codes_and_1800_to_2000_dates")
    project = TOOL_ROOT
    if (
        not root.is_absolute()
        or root.resolve().is_relative_to(project)
        or root.resolve().is_relative_to(checkout.resolve())
        or root.exists()
        or trader.__file__ is None
        or not Path(trader.__file__).resolve().is_relative_to(checkout.resolve())
    ):
        raise ValueError("sample_construction_requires_new_output_and_selected_checkout")
    dates = tuple(date(2018, 1, 1) + timedelta(days=offset) for offset in range(sessions))
    codes = tuple(f"{600000 + offset}" for offset in range(size))
    rows = tuple(
        HistoryTrainingRow(
            code,
            day,
            "main",
            "fixture",
            HistoryTrainingPoint(
                day,
                10.0 + stock + number * 0.01,
                10_000_000.0 + stock * 1000 + number,
                9.9 + stock + number * 0.01,
                10.2 + stock + number * 0.01,
                9.8 + stock + number * 0.01,
                1_000_000.0 + stock + number,
            ),
            "complete",
        )
        for number, day in enumerate(dates)
        for stock, code in enumerate(codes)
    )
    snapshot = HistoryTrainingInputSnapshot(
        "complete_manifest",
        "a" * 64,
        "b" * 64,
        "c" * 64,
        dates[-1],
        dates[-6],
        BaoStockCalendar(dates),
        codes,
        "d" * 64,
    )

    @dataclass
    class _FixtureHistory:
        snapshot: HistoryTrainingInputSnapshot

        def training_row_upper_bound(self, allowed_dates: frozenset[date]) -> int:
            return size * len(allowed_dates)

        def iter_training_rows(
            self, allowed_dates: frozenset[date], progress: Callable[[int], None] | None = None
        ) -> Iterator[HistoryTrainingRow]:
            return (row for row in rows if row.trade_date in allowed_dates)

        def has_industry_window(
            self,
            allowed_dates: frozenset[date],
            required_sessions: int,
            *,
            anchor_dates: frozenset[date] | None = None,
        ) -> bool:
            raise NotImplementedError("sample_construction_probe_does_not_fit_industries")

    source = _FixtureHistory(snapshot)
    root.mkdir(parents=True)
    profiler = None
    if profile_functions:
        from cProfile import Profile

        profiler = Profile(timer=time.process_time)
    cases = {}
    for profile in (V2_TRAINING_PROFILE, V3_TRAINING_PROFILE, V5_TRAINING_PROFILE):
        settings = LoadProfileTrainingSettings(checkout, profile.profile_id)
        split = BuildTrainingDateSplit(
            dates,
            parent_manifest_hash=snapshot.snapshot_hash,
            fit_dates=settings.fit_dates,
            early_stopping_sessions=20 if profile.algorithm != "ridge" else 0,
            history_sessions=profile.history_sessions,
            recent_fit=profile.algorithm == "tcn",
            history_excludes_signal=profile.algorithm == "tcn",
        )
        path = root / f"{profile.profile_id}.sqlite3"
        started = time.perf_counter()
        cpu_started = time.process_time()
        with SQLiteTrainingSampleIndex(
            path,
            profile.heads[0].feature_manifest.names,
            sequence_layout=SampleSequenceLayout(6, 60, 6) if profile.algorithm == "tcn" else None,
            sqlite_policy=sqlite_policy,
            **sample_arguments,
        ) as index:
            with profiler if profiler is not None else nullcontext():
                BuildTrainingSamples(TrainingSampleBuildRequest(
                    source,
                    codes,
                    TomorrowTrainingWindow(split),
                    index,
                    profile,
                ))
        seconds = time.perf_counter() - started
        cpu_seconds = time.process_time() - cpu_started
        digest = hashlib.sha256()
        count = 0
        with SqliteSession(path, SqliteConnectionOptions(read_only=True), sqlite_policy=sqlite_policy) as connection:
            tables = tuple(row[0] for row in connection.run("SELECT name FROM sqlite_master WHERE type=\"table\" ORDER BY name"))
            for table in tables:
                digest.update(table.encode())
                for row in connection.run("SELECT * FROM \"" + table.replace('"', '""') + "\""):
                    digest.update(repr(tuple(row)).encode())
                    count += 1
        cases[profile.profile_id] = {
            "seconds": seconds, "cpu_seconds": cpu_seconds, "numeric_hash": digest.hexdigest(), "table_rows": count,
        }
    v1_settings = LoadV1TrainingSettings(checkout)
    splits = (
        BuildLearnedRankDateSplit(dates, v1_settings, maturity_sessions=1),
        BuildLearnedRankDateSplit(dates, v1_settings, maturity_sessions=5),
    )
    path = root / "v1.sqlite3"
    started = time.perf_counter()
    cpu_started = time.process_time()
    with closing(LearnedRankSampleIndex(path, sqlite_policy=sqlite_policy, **sample_arguments)) as v1_index:
        with profiler if profiler is not None else nullcontext():
            counts = BuildLearnedRankSamples(source, splits, v1_index, history_sessions=v1_settings.history_sessions)
    seconds = time.perf_counter() - started
    cases["v1"] = {
        "seconds": seconds, "cpu_seconds": time.process_time() - cpu_started,
        "numeric_hash": hashlib.sha256(path.read_bytes()).hexdigest(), "samples": counts.samples,
    }
    report: dict[str, object] = {
        "scope": "deterministic_sample_construction_no_model_fitting",
        "fixture": True,
        "sample_policy_medium": medium,
        "stocks": size,
        "calendar_dates": sessions,
        "source_rows": len(rows),
        "cases": cases,
        "models_trained": 0,
        "shared_training_job": False,
        "training_configuration_sha256": hashlib.sha256(
            b"".join((checkout / "config" / name).read_bytes() for name in ("training.json", "features.json"))
        ).hexdigest(),
        "implementation_hash": TrainingImplementationIdentity(),
        "peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024,
    }
    if profiler is not None:
        report["function_profile"] = _project_function_profile(profiler, checkout)
    return report


def _project_function_profile(profiler: Profile, checkout: Path) -> dict[str, object]:
    from types import CodeType

    entries = profiler.getstats()
    functions: list[dict[str, object]] = []
    for entry in entries:
        code = entry.code
        if isinstance(code, CodeType):
            filename, line, name = code.co_filename, code.co_firstlineno, code.co_name
        else:
            filename, line, name = "~", 0, code
        origin, relative = "runtime", None
        if Path(filename).is_absolute():
            source = Path(filename).resolve()
            for label, directory in (("checkout", checkout.resolve()), ("tool", TOOL_ROOT.resolve())):
                if source.is_relative_to(directory):
                    path = source.relative_to(directory)
                    if path.parts and path.parts[0] in {"src", "scripts"}:
                        origin, relative = label, path.as_posix()
                    break
        if re.fullmatch(r"[A-Za-z_][A-Za-z_0-9.]*|<(?:lambda|listcomp|dictcomp|setcomp|genexpr|module)>", name):
            function = name
        else:
            match = re.search(r"(?:method|function) '?([A-Za-z_][A-Za-z_0-9.]*)", name)
            function = match.group(1) if match is not None else "native"
        functions.append({
            "source": origin,
            "path": relative,
            "line": line if relative is not None else None,
            "function": function[:160],
            "calls": entry.callcount,
            "primitive_calls": entry.callcount - entry.reccallcount,
            "self_cpu_seconds": entry.inlinetime,
            "cumulative_cpu_seconds": entry.totaltime,
        })
    functions.sort(key=lambda row: -cast(float, row["self_cpu_seconds"]))
    return {
        "enabled": True,
        "fixture": True,
        "instrumented": True,
        "scope": "enabled_during_sample_builders; sequential_fixture; cProfile_overhead_included; no_model_fitting",
        "timer": "process_cpu_seconds; includes_CPU_of_all_process_threads",
        "event_scope": "interpreter_profile_events; thread_coverage_depends_on_python_runtime",
        "python_implementation": platform.python_implementation(),
        "python_version": platform.python_version(),
        "thread_isolation": False,
        "source_paths": "relative_to_checkout_or_tool; external_runtime_paths_omitted",
        "sort": "self_cpu_seconds_descending",
        "limit": 30,
        "function_count": len(functions),
        "total_calls": sum(entry.callcount for entry in entries),
        "primitive_calls": sum(entry.callcount - entry.reccallcount for entry in entries),
        "total_self_cpu_seconds": sum(entry.inlinetime for entry in entries),
        "cumulative_times_overlap": True,
        "top_functions": functions[:30],
        "performance_evidence": False,
        "production_authority": False,
    }



def MeasureSamples(
    checkout: Path,
    root: Path,
    size: int,
    sessions: int,
    *,
    medium: DiskKind,
    sqlite_policy: SqliteIoPolicy | None,
) -> dict[str, object]:
    RequireIndependentOutput(root, TOOL_ROOT)
    RequireIndependentOutput(root, checkout)
    # The two explicitly selected offline checkouts have different physical APIs.
    # No compatibility selection is installed in the application.
    sqlite_arguments: _SqliteArguments = {"sqlite_policy": sqlite_policy} if sqlite_policy is not None else {}
    sample_arguments = SampleArgumentsForCheckout(checkout, medium=medium)
    import numpy as np
    import trader.training.infra.sample_index as sample_module

    if not Path(sample_module.__file__).resolve().is_relative_to(checkout.resolve()):
        raise ValueError("training_sample_worker_wrong_checkout")

    from trader.recommendation.domain.publication.models import Strategy
    from trader.training.domain.training_dates import BuildTrainingDateSplit
    from trader.training.infra.profile.v1.samples import LearnedRankSampleIndex, _Candidate, _PendingDay
    from trader.training.infra.profile.v2.contracts import V2_TRAINING_PROFILE
    from trader.training.infra.profile.v3.contracts import V3_TRAINING_PROFILE
    from trader.training.infra.profile.v5.contracts import V5_TRAINING_PROFILE
    from trader.training.infra.sample_index import SQLiteTrainingSampleIndex, TrainingIndustryData, TrainingSample
    from trader.training.infra.sample_sequences import SampleSequenceLayout
    from trader.training.infra.implementation_identity import TrainingImplementationIdentity
    from trader.infra.sqlite_connection import SqliteConnection

    counters = {"connections": 0, "closed_connections": 0, "sql_statements": 0, "transactions": 0}
    original_init, original_close = SqliteConnection.__init__, SqliteConnection.close

    def trace(sql: str) -> None:
        counters["sql_statements"] += 1
        counters["transactions"] += int(sql.lstrip().upper().startswith("BEGIN"))

    def open_connection(self: SqliteConnection, driver: sqlite3.Connection, policy: SqliteIoPolicy | None = None) -> None:
        if policy is None:
            original_init(self, driver)
        else:
            original_init(self, driver, policy)
        counters["connections"] += 1
        self.observe_statements(trace)

    def close_connection(self: SqliteConnection) -> None:
        original_close(self)
        counters["closed_connections"] += 1

    # Bounded diagnostic process only; application owners and APIs are unchanged.
    SqliteConnection.__init__ = open_connection  # type: ignore[method-assign]
    SqliteConnection.close = close_connection  # type: ignore[method-assign]

    named = (checkout / "src/trader/training/infra/sample_schema.py").is_file()
    dates = tuple(date(2020, 1, 1) + timedelta(days=i) for i in range(2000))
    split = BuildTrainingDateSplit(dates, parent_manifest_hash="a" * 64)
    selected = (
        *split.model_fit_dates[:sessions], *split.early_stopping_dates[:32],
        *split.calibration_dates[:32], *split.confirmation_dates[:32],
    )
    root.mkdir(parents=True, exist_ok=False)
    cases: dict[str, object] = {}
    for profile in (V2_TRAINING_PROFILE, V3_TRAINING_PROFILE, V5_TRAINING_PROFILE):
        names = profile.heads[0].feature_manifest.names
        layout = SampleSequenceLayout(6, 60, 6) if profile.profile_id == "v5" else None
        channels = np.random.default_rng(289).normal(size=(size, len(selected) + 60, 6)) if layout else None
        path = root / f"{profile.profile_id}.sqlite3"
        started = time.perf_counter()
        with SQLiteTrainingSampleIndex(path, names if named else len(names), sequence_layout=layout, **sqlite_arguments, **sample_arguments) as samples:  # type: ignore[arg-type]  # Explicit historical checkout API.
            for index, day in enumerate(selected):
                rows = []
                for stock in range(size):
                    features = tuple((stock * 7 + index * 3 + i) / 1000 for i in range(6 if layout else len(names)))
                    if channels is not None:
                        features += tuple(float(value) for value in channels[stock, index:index + 60].ravel())
                    targets = tuple(None if (stock + index + i) % 37 == 0 else (stock - i) / 10000 for i in range(6))
                    rows.append(TrainingSample(f"{600000 + stock}", day, "main", "fixture", 1e7, features, (targets[0], targets[1], targets[2], targets[3], targets[4], targets[5])))
                samples.add_final(rows)
            samples.prepare_for_model_fitting(split)
        write_seconds = time.perf_counter() - started
        digest = hashlib.sha256()
        started = time.perf_counter()
        with SQLiteTrainingSampleIndex(
            path, names if named else len(names), sequence_layout=layout, prepared_split_hash=split.content_hash, **sqlite_arguments, **sample_arguments  # type: ignore[arg-type]
        ) as samples:
            for head in profile.heads:
                digest.update(head.model_id.encode())
                counts = samples.industry_counts(head)[0]
                data = samples.industry_data(counts, head)
                with data if named else nullcontext(cast(TrainingIndustryData, data)) as matrix:
                    for part in (matrix.training, matrix.early_stopping, matrix.calibration):
                        digest.update(part.features.tobytes())
                        digest.update(part.labels.tobytes())
                    digest.update(str(matrix.calibration_dates).encode())
                for batch in samples.iter_batches("fixture", head, "validation"):
                    digest.update(batch.features.tobytes())
                    digest.update(batch.labels.tobytes())
                    digest.update(str(batch.trade_dates).encode())
        cases[profile.profile_id] = {
            "write_seconds": write_seconds, "read_seconds": time.perf_counter() - started,
            "numeric_hash": digest.hexdigest(), "database_bytes": path.stat().st_size,
        }
    path = root / "v1.sqlite3"
    started = time.perf_counter()
    with ExitStack() as resources:
        rank_index = LearnedRankSampleIndex(path, **sqlite_arguments, **sample_arguments)
        resources.callback(rank_index.close)
        for number, day in enumerate(selected):
            pending = _PendingDay(day, number, {
                f"{600000 + stock}": _Candidate(
                    f"{600000 + stock}", "fixture",
                    tuple(-0.0 if i == 0 else (number + stock + i) / 1000 for i in range(10)),
                    10.0, [None if (stock + number + i) % 37 == 0 else (stock - i) / 10000 for i in range(5)],
                ) for stock in range(size)
            })
            rank_index.add_day(pending)
        write_seconds = time.perf_counter() - started
        started = time.perf_counter()
        digest = hashlib.sha256()
        with ExitStack() as matrices:
            if named:
                from trader.training.infra.profile.v1.samples import LearnedRankMatrixRequest

                pair = rank_index.read_matrices(
                    tuple(LearnedRankMatrixRequest(selected, strategy) for strategy in (Strategy.TOMORROW, Strategy.D25)),
                    root / "matrices", matrices,
                )
            else:
                pair = tuple(
                    rank_index.matrix(selected, strategy, root / strategy.value, matrices)  # type: ignore[attr-defined]
                    for strategy in (Strategy.TOMORROW, Strategy.D25)
                )
            for rank_matrix in pair:
                digest.update(rank_matrix.features.tobytes())
                digest.update(rank_matrix.targets.tobytes())
                digest.update(str((rank_matrix.codes, rank_matrix.dates, rank_matrix.industries, rank_matrix.groups)).encode())
        cases["v1"] = {
            "write_seconds": write_seconds, "read_seconds": time.perf_counter() - started,
            "numeric_hash": digest.hexdigest(), "database_bytes": path.stat().st_size,
        }
    usage = resource.getrusage(resource.RUSAGE_SELF)
    if counters["connections"] != counters["closed_connections"]:
        raise RuntimeError("training_sample_connection_leak")
    return {
        "cases": cases, "peak_rss_bytes": usage.ru_maxrss * 1024,
        "sample_policy_medium": medium,
        "io_read_blocks": usage.ru_inblock, "io_write_blocks": usage.ru_oublock,
        "minor_page_faults": usage.ru_minflt, "major_page_faults": usage.ru_majflt,
        "database": counters, "implementation_hash": TrainingImplementationIdentity(),
        "python_version": platform.python_version(), "numpy_version": np.__version__,
    }


def Main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--size", type=int, required=True)
    parser.add_argument("--sessions", type=int, required=True)
    parser.add_argument("--medium", choices=("hdd", "ssd"), required=True)
    parser.add_argument("--construct-features", action="store_true")
    parser.add_argument("--profile-functions", action="store_true")
    parser.add_argument(
        "--computation-candidate", choices=("baseline", "indexed_anchors", "schema_cache", "combined"), default="baseline"
    )
    parser.add_argument("--runtime-config", type=Path)
    args = parser.parse_args()
    RequireIndependentOutput(args.root, TOOL_ROOT)
    RequireIndependentOutput(args.root, args.checkout)
    if not args.construct_features and args.computation_candidate != "baseline":
        raise ValueError("computation_candidate_requires_feature_construction")
    if args.profile_functions and not args.construct_features:
        raise ValueError("function_profile_requires_feature_construction")
    sqlite_policy = None
    if (args.checkout / "src/trader/infra/sqlite_policy.py").is_file():
        from trader.bootstrap import BuildSqlitePolicy
        from trader.infra.settings import LoadRuntimeSettings

        sqlite_policy = BuildSqlitePolicy(LoadRuntimeSettings(args.runtime_config or args.checkout / "config/runtime.json"))
    if args.construct_features:
        if sqlite_policy is None:
            raise ValueError("sample_construction_requires_injected_sqlite_policy")
        report = _measure_feature_construction(
            args.checkout,
            args.root,
            args.size,
            args.sessions,
            medium=args.medium,
            sqlite_policy=sqlite_policy,
            computation_candidate=cast("ComputationCandidate", args.computation_candidate),
            profile_functions=args.profile_functions,
        )
    else:
        report = MeasureSamples(
            args.checkout,
            args.root,
            args.size,
            args.sessions,
            medium=args.medium,
            sqlite_policy=sqlite_policy,
        )
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0
