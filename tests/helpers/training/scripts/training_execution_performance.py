"""Execution benchmark admission uses frozen four-profile, three-workload evidence."""

from __future__ import annotations
import hashlib
import json
import math
from collections.abc import Generator, Sequence
from dataclasses import replace
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING
from pytest import MonkeyPatch
from scripts.core.contracts import DiagnosticResult
from scripts.training.jobs.training_performance import SAMPLE_PREPARATION_CONTROLLED_CAUSE
from scripts.training.options import TrainingOptions

if TYPE_CHECKING:
    from scripts.download.history.history_write_benchmark import HistoryFixtureSupplier
    from trader.download.domain.baostock_daily import BaoStockDailyCell
    from trader.download.domain.history_revision import HistoryTrainingPoint

_PROFILES = ("v1", "v2", "v3", "v5")

_STRATEGIES = ("tomorrow", "d25")

_MODES = ("baseline_before", "candidate", "baseline_after")
_D25_TARGET = {"identity": "mean_close_gain_d2_d5_at_least_100_bps", "threshold_bps": 100}
_TOMORROW_TARGET = "next_close_gain_at_least_500_bps"


def _d25_fixture_point(day: date, index: int, stock: int) -> HistoryTrainingPoint:
    from trader.download.domain.history_revision import HistoryTrainingPoint

    def price(position: int) -> float:
        phase = math.tau * (position + 5 * stock) / (32 + stock)
        return (20 + stock / 10) * math.exp(0.15 * math.sin(phase) + 0.0001 * position)

    # Multi-day trends last longer than the D25 window. The staggered phases
    # preserve positive and negative labels and rank variation in each split.
    preclose, close = price(index - 1), price(index)
    opening = preclose * 1.001
    high = max(opening, close) * 1.005
    low = min(opening, close) * 0.995
    volume = 1_000_000.0 + 100 * index + 1000 * stock
    amount = (opening + close) / 2 * volume
    return HistoryTrainingPoint(
        day, 0.9 * close, amount, 0.9 * opening, 0.9 * high, 0.9 * low, volume,
        raw_open=opening, raw_high=high, raw_low=low, raw_close_price=close, raw_preclose=preclose, raw_amount=amount,
    )


def _install_published_d25_history_prices(monkeypatch: MonkeyPatch) -> None:
    from scripts.download.history.history_write_benchmark import HistoryFixtureSupplier

    original = HistoryFixtureSupplier._varying_cell

    def varying_cell(supplier: HistoryFixtureSupplier, code: str, day: date) -> BaoStockDailyCell:
        cell = original(supplier, code, day)
        assert cell.unadjusted is not None and cell.qfq is not None
        point = _d25_fixture_point(day, (day - date(2024, 1, 1)).days, int(code) - 600_000)
        assert point.raw_close_price is not None and point.raw_preclose is not None
        raw = replace(
            cell.unadjusted, open_price=point.raw_open, high_price=point.raw_high, low_price=point.raw_low,
            close_price=point.raw_close_price, volume=point.raw_volume, amount=point.raw_amount,
            preclose=point.raw_preclose, pct_change=(point.raw_close_price / point.raw_preclose - 1) * 100,
        )
        qfq = replace(
            cell.qfq, open_price=point.qfq_open, high_price=point.qfq_high, low_price=point.qfq_low,
            close_price=point.qfq_close_price, volume=point.raw_volume, amount=point.qfq_amount,
        )
        return replace(cell, unadjusted=raw, qfq=qfq)

    monkeypatch.setattr(HistoryFixtureSupplier, "_varying_cell", varying_cell)


def _install_tomorrow_history(monkeypatch: MonkeyPatch, dates: tuple[date, ...], snapshot_hash: str) -> None:
    """Supply the event classes required by a benchmark fixture focused on D25 input and cache handling."""
    from trader.download.domain.history_revision import HistoryTrainingPoint, HistoryTrainingRow
    from trader.training.infra import selected_training
    from trader.training.infra.tomorrow.training import TomorrowHistoryReads

    codes = tuple(f"600{index:03d}" for index in range(1, 9))
    first = dates[0]

    def close(index: int, stock: int) -> float:
        return 106.0 if index % 4 == (1 if stock % 2 == 0 else 3) else 100.0

    def rows(selected: tuple[date, ...], reads: TomorrowHistoryReads) -> Generator[HistoryTrainingRow, None, None]:
        reads.scans += 1
        for day in selected:
            index = (day - first).days
            for stock, code in enumerate(codes):
                current = close(index, stock)
                opening = current * (0.94 if close(index + 1, stock) == 106 else 1.02)
                high, low = max(current, opening) + 1, min(current, opening) - 1
                reads.rows += 1
                yield HistoryTrainingRow(code, day, "main", None, HistoryTrainingPoint(
                    day, current, 20_000_000, opening, high, low, 10_000,
                    raw_open=opening, raw_high=high, raw_low=low, raw_close_price=current,
                    raw_preclose=close(index - 1, stock),
                ), "complete")

    def open_history(*_args: object, **_options: object) -> SimpleNamespace:
        reads = TomorrowHistoryReads()
        return SimpleNamespace(open_dates=dates, universe_count=len(codes), snapshot_hash=snapshot_hash, reads=reads,
                               iter_training_rows=lambda selected: rows(selected, reads))

    monkeypatch.setattr(selected_training, "OpenTomorrowHistoryInput", open_history)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _mapping(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return value


def _records(value: object) -> list[dict[str, object]]:
    assert isinstance(value, list)
    return [_mapping(item) for item in value]


def _summary(result: DiagnosticResult) -> dict[str, object]:
    assert result.payload is not None
    return _mapping(result.payload["summary"])


def _fingerprints(profiles: Sequence[str], *, d25_only: bool) -> list[dict[str, object]]:
    return [
        {"profile": profile, "strategy": strategy, "model_values_and_metrics_sha256": _digest(profile + strategy)}
        for profile in profiles
        for strategy in (("d25",) if d25_only else _STRATEGIES)
    ]


def _profile_results(mode: str, profiles: Sequence[str], reused: bool) -> list[dict[str, object]]:
    return [
        {
            "profile": profile,
            "status": "engineering_ready",
            "training_input_hash": _digest("input:" + profile),
            "d25_target": dict(_D25_TARGET),
            "reused": reused,
            "post_commit_issues": [],
            "delivery_issues": [],
            "heads": [
                {
                    "strategy": "d25",
                    "status": "engineering_ready",
                    "model_hash": _digest(mode + profile + ":utility"),
                    "report_hash": _digest(mode + profile + ":report"),
                }
            ],
        }
        for profile in profiles
    ]


def _terminal_shutdown(profiles: Sequence[str]) -> dict[str, object]:
    return {
        "shutdown": {
            "name": "training_executor", "completed": True, "timed_out": False, "cancelled_count": 0,
            "failure": None, "watchdog_started": True, "watchdog_failure": None,
        },
        "progress_shutdown": [
            {"profile": profile, "name": "progress_heartbeat", "completed": True, "timed_out": False,
             "cancelled_count": 0, "detail": ""}
            for profile in profiles
        ],
    }


def _run(
    mode: str, profiles: Sequence[str], seconds: float, reused: bool, *, d25_only: bool = False,
) -> dict[str, object]:
    outcomes = _profile_results(mode, profiles, reused)
    artifacts = []
    tomorrow = []
    for outcome in outcomes:
        profile = str(outcome["profile"])
        utility = _records(outcome["heads"])[0]
        artifacts.append({
            "profile": profile, "strategy": "d25", "target": _D25_TARGET["identity"],
            "input_hash": outcome["training_input_hash"], "model_hash": _digest(mode + profile + ":bound"),
            "utility_model_hash": utility["model_hash"], "utility_report_hash": utility["report_hash"],
        })
        if not d25_only:
            artifacts.append({
                "profile": profile, "strategy": "tomorrow", "target": _TOMORROW_TARGET,
                "input_hash": _digest("history"), "model_hash": _digest(mode + profile + ":tomorrow"),
            })
            tomorrow.append({
                "profile": profile, "target": _TOMORROW_TARGET, "threshold_bps": 500, "ready": True,
                "history_snapshot_hash": _digest("history"), "model_identity": _digest(mode + profile + ":tomorrow"),
                "assessment_rows": 0 if reused else 100,
            })
    return {
        "complete": True,
        "exit_code": 0,
        "reloaded_heads": len(artifacts),
        "seconds": seconds,
        "profiles": outcomes,
        "artifacts": artifacts,
        "numeric_fingerprints": _fingerprints(profiles, d25_only=d25_only),
        "execution": {
            "status": "completed",
            "training_status": "completed",
            "strategy": "d25" if d25_only else None,
            "profiles": list(profiles),
            "results": [
                {"profile": profile, "exit_code": 0, "post_commit_issues": [], "delivery_issues": []}
                for profile in profiles
            ],
            "not_started": [],
            "history_scan_passes": 0 if reused or d25_only else 3,
            "history_rows_read": 0 if reused or d25_only else 400,
            "history_reads_by_strategy": {
                **({} if d25_only else {"tomorrow": {"scans": 0 if reused else 2, "rows": 0 if reused else 200}}),
                "d25": {"scans": 0 if reused or d25_only else 1, "rows": 0 if reused or d25_only else 200},
            },
            **_terminal_shutdown(profiles),
            **({"tomorrow_probability": tomorrow} if not d25_only else {}),
        },
    }


def _sample_scenario(mode: str, seconds: float, profiles: Sequence[str] = _PROFILES) -> dict[str, object]:
    selected = tuple(profile for profile in profiles if profile != "v1")
    excluded = [profile for profile in profiles if profile == "v1"]
    if not selected:
        return {"profiles": [], "not_applicable_profiles": excluded, "run": None}
    run = _run(mode, selected, seconds, False, d25_only=True)
    return {
        "profiles": list(selected),
        "not_applicable_profiles": excluded,
        "cache_verified": True,
        "source_cache_unchanged": True,
        "target_cache_cleared": True,
        "cache_transfer_method": "copy",
        "copied_bytes": 4096,
        "linked_bytes": 0,
        "model_files_copied": 0,
        "copy_verification_seconds": 0.25,
        "preparation": {
            "method": "controlled_failure_before_fit",
            "fault_injected": True,
            "fitter_restored": True,
            "fit_entry_counts": dict.fromkeys(selected, 1),
            "unexpected_workspaces": 0,
            "model_files_created": 0,
            "exit_code": 1,
            "seconds": 1.0,
            "execution": {
                "status": "failed", "training_status": "failed", "strategy": "d25", "profiles": list(selected),
                "history_scan_passes": 1, "history_rows_read": 400,
                **_terminal_shutdown(selected),
            },
            "profiles": [
                {
                    "profile": profile,
                    "training_input_hash": _digest("input:" + profile),
                    "d25_target": dict(_D25_TARGET),
                    "reused": False,
                    "status": "blocked",
                    "heads": [
                        {
                            "strategy": "d25", "status": "blocked", "model_hash": "", "report_hash": "",
                            "failure_reasons": ["profile_training_failed"],
                            "diagnostic": {"causes": [
                                {"code": "profile_training_failed", "error_type": "ProfileTrainingOperationError"},
                                {"code": SAMPLE_PREPARATION_CONTROLLED_CAUSE, "error_type": "ValueError"},
                            ]},
                        }
                    ],
                }
                for profile in selected
            ],
        },
        "run": run,
    }


def _result(number: int, mode: str, ratios: tuple[float, float, float]) -> DiagnosticResult:
    candidate = mode == "candidate"
    factor = 1.1 if mode == "baseline_before" else 1.0
    initial, sample, repeat = ratios if candidate else (factor, factor, factor)
    summary = {
        "initial": _run(mode, _PROFILES, 100.0 * initial, False),
        "repeat": _run(mode, _PROFILES, 2.0 * repeat, True),
        "sample_reuse": _sample_scenario(mode, 20.0 * sample),
        "source_identity_unchanged": True,
        "source_files_unchanged": True,
        "source_data_writes": 0,
        "training_configuration_sha256": _digest("configuration"),
        "training_implementation_sha256": _digest("candidate" if candidate else "baseline"),
        "execution_candidate": {
            "execution": "combined" if candidate else "baseline",
            "cpu": "two_plus_one" if candidate else "baseline",
            "production_enabled": False,
            "performance_evidence": False,
            "runs": [{
                "profiles": list(profiles), "cpu_capacity": 3, "native_threads": 1 if candidate else 2,
                "profile_threads": {"v1": 2, "v2": 2, "v3": 2}, "memory_capacity_bytes": 1024,
                "batch_rows": 256, "queue_batches": 2,
            } for profiles in (_PROFILES, _PROFILES, _PROFILES[1:], _PROFILES[1:])],
            **({
                "candidate_source_hash": _digest("execution-candidate"),
                "options": {
                    "memory_first": True, "release_cpu_before_publish": True, "shared_matrix_memory": True,
                    "shared_validation": True, "normalized_tcn_batches": True,
                },
            } if candidate else {}),
        },
        "computation_candidate": {"name": "baseline", "production_enabled": False, "performance_evidence": False},
        "scheduling_candidate": {"name": "baseline", "production_enabled": False, "performance_evidence": False},
        "process_peak_rss_bytes": 512 * 1024 * 1024,
    }
    # Exercise the JSON adapter's actual lists and primitives, including independent child payloads.
    payload: object = json.loads(json.dumps({"status": "passed", "summary": summary}, allow_nan=False))
    return DiagnosticResult(
        f"training_alignment:round_{number}:{mode}:v1,v2,v3,v5", 0, 999999.0, _mapping(payload), None,
    )


def _results(
    initial_ratios: Sequence[float] = (0.8, 0.85, 0.9, 0.95, 1.1),
    sample_ratio: float = 1.05,
    repeat_ratio: float = 1.05,
) -> tuple[DiagnosticResult, ...]:
    return tuple(
        _result(number, mode, (initial, sample_ratio, repeat_ratio))
        for number, initial in enumerate(initial_ratios, 1)
        for mode in _MODES
    )


def _options(tmp_path: Path, **changes: object) -> TrainingOptions:
    baseline = tmp_path / "baseline"
    (baseline / "src/trader").mkdir(parents=True)
    options = TrainingOptions(
        action_identity="training-performance",
        history_root=tmp_path / "history",
        training_probe_directory=tmp_path / "work",
        baseline_checkout=baseline,
        execution_candidate="combined",
        cpu_candidate="two_plus_one",
        training_benchmark_rounds=5,
    )
    return replace(options, **changes)
