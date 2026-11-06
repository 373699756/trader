"""Execution benchmark admission uses frozen four-profile, three-workload evidence."""

from __future__ import annotations
import hashlib
import json
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from scripts.core.contracts import DiagnosticResult
from scripts.training.options import TrainingOptions

_PROFILES = ("v1", "v2", "v3", "v5")

_STRATEGIES = ("tomorrow", "d25")

_MODES = ("baseline_before", "candidate", "baseline_after")


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


def _fingerprints(profiles: Sequence[str]) -> list[dict[str, object]]:
    return [
        {"profile": profile, "strategy": strategy, "model_values_and_metrics_sha256": _digest(profile + strategy)}
        for profile in profiles
        for strategy in _STRATEGIES
    ]


def _profile_results(mode: str, profiles: Sequence[str], reused: bool) -> list[dict[str, object]]:
    return [
        {
            "profile": profile,
            "status": "engineering_ready",
            "training_input_hash": _digest("input:" + profile),
            "reused": reused,
            "heads": [
                {
                    "strategy": strategy,
                    "status": "engineering_ready",
                    "model_hash": _digest(mode + profile + strategy + ":model"),
                    "report_hash": _digest(mode + profile + strategy + ":report"),
                }
                for strategy in _STRATEGIES
            ],
        }
        for profile in profiles
    ]


def _run(mode: str, profiles: Sequence[str], seconds: float, reused: bool) -> dict[str, object]:
    return {
        "complete": True,
        "seconds": seconds,
        "profiles": _profile_results(mode, profiles, reused),
        "numeric_fingerprints": _fingerprints(profiles),
        "execution": {
            "status": "completed",
            "profiles": list(profiles),
            "history_scan_passes": 0 if reused else 1,
            "history_rows_read": 0 if reused else 400,
        },
    }


def _sample_scenario(mode: str, seconds: float) -> dict[str, object]:
    profiles = _PROFILES[1:]
    run = _run(mode, profiles, seconds, False)
    run["execution"] = {
        "status": "completed", "profiles": list(profiles), "history_scan_passes": 0, "history_rows_read": 0,
    }
    return {
        "profiles": list(profiles),
        "not_applicable_profiles": ["v1"],
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
            "fit_entry_counts": dict.fromkeys(profiles, 1),
            "unexpected_workspaces": 0,
            "model_files_created": 0,
            "exit_code": 1,
            "seconds": 1.0,
            "execution": {
                "status": "failed", "profiles": list(profiles), "history_scan_passes": 1, "history_rows_read": 400,
            },
            "profiles": [
                {
                    "profile": profile,
                    "training_input_hash": _digest("input:" + profile),
                    "reused": False,
                    "status": "blocked",
                    "heads": [
                        {
                            "strategy": strategy, "status": "blocked", "model_hash": "", "report_hash": "",
                            "failure_reasons": ["profile_training_failed"],
                        }
                        for strategy in _STRATEGIES
                    ],
                }
                for profile in profiles
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
