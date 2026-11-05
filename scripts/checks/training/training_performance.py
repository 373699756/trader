"""Plan fresh, directly supervised training processes for comparable benchmark rounds."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TypedDict

from scripts.checks.contracts import DiagnosticCommand, DiagnosticOptions, DiagnosticResult


class _Round(TypedDict):
    mode: str
    complete: bool
    seconds: float | None
    repeat_seconds: float | None
    peak_rss_bytes: int | None
    reuse_failures: tuple[str, ...]
    sample_reuse_seconds: float | None
    sample_reuse_failures: tuple[str, ...]


class _CheckoutRound(TypedDict):
    mode: str
    complete: bool
    initial_seconds: float | None
    repeat_seconds: float | None
    repeat_profiles: object
    repeat_execution: object
    peak_rss_bytes: object
    reuse_failures: tuple[str, ...]
    sample_reuse: object
    sample_reuse_failures: tuple[str, ...]
    sample_reuse_seconds: float | None


def TrainingReuseFailures(initial: object, repeat: object, profiles: Sequence[str]) -> tuple[str, ...]:
    """Check explicit execution and immutable artifact evidence at the JSON boundary."""
    if not isinstance(initial, Mapping) or not isinstance(repeat, Mapping):
        return ("reuse_run_missing",)
    failures: list[str] = []
    if initial.get("complete") is not True or repeat.get("complete") is not True:
        failures.append("reuse_run_incomplete")
    execution = repeat.get("execution")
    if not isinstance(execution, Mapping) or execution.get("status") != "completed":
        failures.append("reuse_execution_missing_or_failed")
    else:
        for key in ("history_scan_passes", "history_rows_read"):
            value = execution.get(key)
            if type(value) is not int or value != 0:
                failures.append("reuse_" + key + "_not_zero")
        selected = execution.get("profiles")
        if (not isinstance(selected, (list, tuple)) or not all(isinstance(item, str) for item in selected)
                or tuple(sorted(selected)) != tuple(sorted(profiles))):
            failures.append("reuse_execution_profiles_mismatch")
    before = _reuse_identity(initial.get("profiles"), profiles)
    after = _reuse_identity(repeat.get("profiles"), profiles)
    if before is None or after is None or before != after:
        failures.append("reuse_artifact_identity_mismatch")
    runs = repeat.get("profiles")
    if not isinstance(runs, list) or not runs or any(
        not isinstance(run, Mapping) or run.get("reused") is not True for run in runs
    ):
        failures.append("reuse_profiles_not_reused")
    return tuple(failures)


def TrainingSampleReuseFailures(initial: object, scenario: object, profiles: Sequence[str]) -> tuple[str, ...]:
    """Require a real refit from copied sealed samples, with no raw history scan."""
    if not isinstance(initial, Mapping) or not isinstance(scenario, Mapping):
        return ("sample_reuse_run_missing",)
    selected = tuple(profile for profile in profiles if profile != "v1")
    excluded = tuple(profile for profile in profiles if profile == "v1")
    if scenario.get("profiles") not in (selected, list(selected)) or scenario.get("not_applicable_profiles") not in (
        excluded, list(excluded)
    ):
        return ("sample_reuse_profiles_mismatch",)
    if not selected:
        return () if scenario.get("run") is None else ("sample_reuse_v1_not_applicable",)
    run = scenario.get("run")
    if not isinstance(run, Mapping):
        return ("sample_reuse_run_missing",)
    failures = list(TrainingSamplePreparationFailures(initial, scenario.get("preparation"), selected))
    if initial.get("complete") is not True or run.get("complete") is not True:
        failures.append("sample_reuse_run_incomplete")
    execution = run.get("execution")
    if not isinstance(execution, Mapping) or execution.get("status") != "completed":
        failures.append("sample_reuse_execution_missing_or_failed")
    else:
        for key in ("history_scan_passes", "history_rows_read"):
            value = execution.get(key)
            if type(value) is not int or value != 0:
                failures.append("sample_reuse_" + key + "_not_zero")
        if execution.get("profiles") not in (selected, list(selected)):
            failures.append("sample_reuse_execution_profiles_mismatch")
    runs = run.get("profiles")
    baseline = initial.get("profiles")
    if not isinstance(baseline, list) or not isinstance(runs, list):
        failures.append("sample_reuse_profile_results_missing")
    else:
        before = [item for item in baseline if isinstance(item, Mapping) and item.get("profile") in selected]
        if _reuse_identity(before, selected) is None or _reuse_identity(runs, selected) is None:
            failures.append("sample_reuse_artifacts_incomplete")
        elif sorted((item["profile"], item["training_input_hash"]) for item in before) != sorted(
            (item["profile"], item["training_input_hash"]) for item in runs
        ):
            failures.append("sample_reuse_input_mismatch")
        if not runs or any(not isinstance(item, Mapping) or item.get("reused") is not False for item in runs):
            failures.append("sample_reuse_did_not_refit")
    fingerprints = initial.get("numeric_fingerprints")
    expected = (
        [item for item in fingerprints if isinstance(item, Mapping) and item.get("profile") in selected]
        if isinstance(fingerprints, (tuple, list)) else None
    )
    values = _fingerprint_values(expected, selected)
    if values is None or values != _fingerprint_values(run.get("numeric_fingerprints"), selected):
        failures.append("sample_reuse_model_values_mismatch")
    if scenario.get("source_cache_unchanged") is not True:
        failures.append("sample_reuse_source_cache_changed")
    if scenario.get("target_cache_cleared") is not True:
        failures.append("sample_reuse_target_cache_not_cleared")
    if scenario.get("cache_verified") is not True:
        failures.append("sample_reuse_cache_unverified")
    copied = scenario.get("copied_bytes")
    if type(copied) is not int or copied <= 0:
        failures.append("sample_reuse_cache_copy_missing")
    if type(scenario.get("model_files_copied")) is not int or scenario.get("model_files_copied") != 0:
        failures.append("sample_reuse_models_copied")
    if _seconds(run.get("seconds")) is None:
        failures.append("sample_reuse_duration_invalid")
    if _seconds(scenario.get("copy_verification_seconds")) is None:
        failures.append("sample_reuse_copy_duration_invalid")
    return tuple(failures)


def TrainingSamplePreparationFailures(
    initial: object, preparation: object, profiles: Sequence[str]
) -> tuple[str, ...]:
    if not isinstance(initial, Mapping) or not isinstance(preparation, Mapping) or not profiles:
        return ("sample_preparation_missing",)
    failures: list[str] = []
    if (preparation.get("method") != "controlled_failure_before_fit"
            or preparation.get("fault_injected") is not True or preparation.get("fitter_restored") is not True):
        failures.append("sample_preparation_fault_scope_invalid")
    counts = preparation.get("fit_entry_counts")
    if (not isinstance(counts, Mapping) or set(counts) != set(profiles)
            or any(type(count) is not int or count != 1 for count in counts.values())):
        failures.append("sample_preparation_fit_entry_missing_or_repeated")
    for key in ("unexpected_workspaces", "model_files_created"):
        if type(preparation.get(key)) is not int or preparation.get(key) != 0:
            failures.append("sample_preparation_" + key + "_not_zero")
    if type(preparation.get("exit_code")) is not int or preparation.get("exit_code") != 1:
        failures.append("sample_preparation_exit_invalid")
    execution = preparation.get("execution")
    if not isinstance(execution, Mapping) or execution.get("status") != "failed":
        failures.append("sample_preparation_failure_result_missing")
    else:
        if execution.get("profiles") not in (tuple(profiles), list(profiles)):
            failures.append("sample_preparation_execution_profiles_mismatch")
        scans, rows = execution.get("history_scan_passes"), execution.get("history_rows_read")
        if type(scans) is not int or scans != 1 or type(rows) is not int or rows <= 0:
            failures.append("sample_preparation_history_scan_invalid")
    baseline, prepared = initial.get("profiles"), preparation.get("profiles")
    if (not isinstance(baseline, list) or not isinstance(prepared, list) or len(prepared) != len(profiles)
            or any(not isinstance(item, Mapping) for item in prepared)):
        failures.append("sample_preparation_profiles_missing")
    else:
        expected = {item.get("profile"): item.get("training_input_hash") for item in baseline
                    if isinstance(item, Mapping) and item.get("profile") in profiles}
        seen = set()
        for item in prepared:
            profile, heads = item.get("profile"), item.get("heads")
            if (not isinstance(profile, str) or profile not in profiles or profile in seen
                    or item.get("status") != "blocked" or item.get("reused") is not False
                    or not expected.get(profile) or item.get("training_input_hash") != expected[profile]
                    or not isinstance(heads, list) or len(heads) != 2
                    or any(not isinstance(head, Mapping) for head in heads)):
                failures.append("sample_preparation_profile_result_invalid")
                continue
            seen.add(profile)
            if ({head.get("strategy") for head in heads} != {"tomorrow", "d25"}
                    or any(head.get("status") != "blocked" or head.get("model_hash") != ""
                           or head.get("report_hash") != "" or head.get("failure_reasons") != ["profile_training_failed"]
                           for head in heads)):
                failures.append("sample_preparation_head_result_invalid")
    if _seconds(preparation.get("seconds")) is None:
        failures.append("sample_preparation_duration_invalid")
    return tuple(failures)


def _reuse_identity(value: object, profiles: Sequence[str]) -> tuple[tuple[str, ...], ...] | None:
    if not profiles or len(set(profiles)) != len(profiles) or not isinstance(value, list) or len(value) != len(profiles):
        return None
    identities: list[tuple[str, ...]] = []
    seen: set[str] = set()
    for run in value:
        if not isinstance(run, Mapping):
            return None
        profile, input_hash, heads = run.get("profile"), run.get("training_input_hash"), run.get("heads")
        if (not isinstance(profile, str) or profile not in profiles or profile in seen
                or not isinstance(input_hash, str) or not input_hash or run.get("status") != "engineering_ready"
                or not isinstance(heads, list) or len(heads) != 2):
            return None
        seen.add(profile)
        strategies: set[str] = set()
        for head in heads:
            if not isinstance(head, Mapping):
                return None
            strategy, model_hash, report_hash = (head.get(key) for key in ("strategy", "model_hash", "report_hash"))
            if (not isinstance(strategy, str) or strategy not in {"tomorrow", "d25"} or strategy in strategies
                    or head.get("status") != "engineering_ready"
                    or not isinstance(model_hash, str) or not model_hash or not isinstance(report_hash, str) or not report_hash):
                return None
            strategies.add(strategy)
            identities.append((profile, input_hash, strategy, model_hash, report_hash))
    return tuple(sorted(identities))


def BuildTrainingBenchmarkCommands(options: DiagnosticOptions, python_executable: str) -> tuple[DiagnosticCommand, ...]:
    if options.history_root is None or options.training_probe_directory is None:
        raise ValueError("training_benchmark_requires_history_and_output")
    project = Path(__file__).resolve().parents[3]
    history, work = options.history_root.resolve(), options.training_probe_directory.resolve()
    if (
        history.is_relative_to(project)
        or work.is_relative_to(project)
        or work.exists()
        or history.is_relative_to(work)
        or work.is_relative_to(history)
    ):
        raise ValueError("training_benchmark_requires_external_history_copy_and_new_output")
    baseline = options.baseline_checkout.resolve(strict=True) if options.baseline_checkout is not None else None
    if baseline is not None and (
        baseline == project or baseline.is_relative_to(project) or not (baseline / "src/trader").is_dir()
    ):
        raise ValueError("training_benchmark_requires_external_product_checkout")
    profiles = options.training_profiles
    commands = []
    modes = (
        ("baseline_before", "candidate", "baseline_after")
        if baseline
        else ("serial_before", "parallel", "serial_after")
    )
    for mode in modes:
        selections = (
            (profiles,) if baseline is not None or mode == "parallel" else tuple((profile,) for profile in profiles)
        )
        checkout = baseline if baseline is not None and mode.startswith("baseline_") else project
        for selected in selections:
            commands.append(
                DiagnosticCommand(
                    "training_alignment:" + mode + ":" + ",".join(selected),
                    (
                        python_executable,
                        "-m",
                        "scripts.diagnose_runtime", "--internal-worker", 'scripts.checks.training.training_alignment', "--",
                        "--history-root",
                        str(history),
                        "--work-root",
                        str(work / (mode + "-" + "_".join(selected))),
                        "--profiles",
                        ",".join(selected),
                        "--benchmark-mode",
                        mode,
                        "--product-checkout",
                        str(checkout),
                        "--runtime-config",
                        str(options.runtime_config.resolve()),
                    ),
                    options.command_timeout_seconds,
                )
            )
    return tuple(commands)


def SummarizeTrainingBenchmark(results: Sequence[DiagnosticResult]) -> dict[str, object]:
    if any(result.name.startswith("training_alignment:baseline_") for result in results):
        return _summarize_checkouts(results)
    modes = ("serial_before", "parallel", "serial_after")
    rounds: list[_Round] = []
    identities = []
    for mode in modes:
        selected = tuple(result for result in results if result.name.startswith("training_alignment:" + mode + ":"))
        ready = bool(selected) and all(
            result.return_code == 0 and result.payload is not None and result.payload.get("status") == "passed"
            for result in selected
        )
        fingerprints = []
        peak = 0
        initial_times = []
        repeat_times = []
        sample_times = []
        reuse_failures: list[str] = []
        sample_failures: list[str] = []
        for result in selected:
            summary = result.payload.get("summary") if result.payload is not None else None
            if not isinstance(summary, Mapping):
                ready = False
                continue
            initial, repeat = summary.get("initial"), summary.get("repeat")
            if not isinstance(initial, Mapping) or not isinstance(repeat, Mapping):
                ready = False
                continue
            initial_seconds, repeat_seconds = _seconds(initial.get("seconds")), _seconds(repeat.get("seconds"))
            if initial_seconds is None or repeat_seconds is None:
                ready = False
            else:
                initial_times.append(initial_seconds)
                repeat_times.append(repeat_seconds)
            ready = ready and bool(initial.get("complete")) and bool(repeat.get("complete"))
            profiles = initial.get("profiles")
            if not isinstance(profiles, list) or not all(isinstance(profile, Mapping) for profile in profiles):
                ready = False
                continue
            expected = result.name.rsplit(":", 1)[-1].split(",")
            reuse_failures.extend(TrainingReuseFailures(initial, repeat, expected))
            scenario = summary.get("sample_reuse")
            sample_failures.extend(TrainingSampleReuseFailures(initial, scenario, expected))
            if isinstance(scenario, Mapping):
                sample_run = scenario.get("run")
                seconds = _seconds(sample_run.get("seconds")) if isinstance(sample_run, Mapping) else None
                if seconds is not None:
                    sample_times.append(seconds)
            if _reuse_identity(profiles, expected) is None:
                ready = False
                continue
            if len(profiles) != len(expected) or {profile.get("profile") for profile in profiles} != set(expected):
                ready = False
            peak = max(peak, int(summary.get("process_peak_rss_bytes") or 0))
            for profile in profiles:
                ready = ready and bool(profile.get("training_input_hash")) and len(profile.get("heads", ())) == 2
                ready = ready and all(
                    head.get("model_hash") and head.get("report_hash") for head in profile.get("heads", ())
                )
                fingerprints.append(
                    (
                        profile["profile"],
                        profile.get("training_input_hash"),
                        tuple(
                            (head["strategy"], head.get("model_hash"), head.get("report_hash"))
                            for head in profile.get("heads", ())
                        ),
                    )
                )
        identities.append(tuple(sorted(fingerprints)))
        rounds.append(
            {
                "mode": mode,
                "complete": ready,
                "seconds": sum(initial_times) if selected and len(initial_times) == len(selected) else None,
                "repeat_seconds": sum(repeat_times) if selected and len(repeat_times) == len(selected) else None,
                "peak_rss_bytes": peak or None,
                "reuse_failures": tuple(sorted(set(reuse_failures))),
                "sample_reuse_seconds": sum(sample_times) if sample_times and not sample_failures else None,
                "sample_reuse_failures": tuple(sorted(set(sample_failures))),
            }
        )
    comparable = (
        all(item["complete"] for item in rounds)
        and all(not item["reuse_failures"] for item in rounds)
        and all(not item["sample_reuse_failures"] for item in rounds)
        and bool(identities[0])
        and identities[0] == identities[1] == identities[2]
    )
    before_seconds, parallel_seconds, after_seconds = (_seconds(item["seconds"]) for item in rounds)
    speedup = (
        min(before_seconds, after_seconds) / parallel_seconds
        if comparable and before_seconds is not None and parallel_seconds is not None and after_seconds is not None
        else None
    )
    return {
        "rounds": rounds,
        "comparable": comparable,
        "speedup_vs_best_serial": speedup,
        "production_authority": False,
        "concurrency_enablement_gate": False,
    }


def _summarize_checkouts(results: Sequence[DiagnosticResult]) -> dict[str, object]:
    rounds: list[_CheckoutRound] = []
    fingerprints = []
    profiles = []
    configuration_hashes = []
    modes = ("baseline_before", "candidate", "baseline_after")
    for mode in modes:
        selected = tuple(result for result in results if result.name.startswith("training_alignment:" + mode + ":"))
        complete = len(selected) == 1
        summary = selected[0].payload.get("summary") if selected and selected[0].payload is not None else None
        if not isinstance(summary, Mapping):
            summary = {}
            complete = False
        initial, repeat = summary.get("initial"), summary.get("repeat")
        configuration_hash = summary.get("training_configuration_sha256")
        configuration_hashes.append(configuration_hash)
        complete = (
            complete
            and isinstance(configuration_hash, str)
            and len(configuration_hash) == 64
            and all(character in "0123456789abcdef" for character in configuration_hash)
        )
        if not isinstance(initial, Mapping) or not isinstance(repeat, Mapping):
            initial, repeat = {}, {}
            complete = False
        runs = initial.get("profiles")
        if not isinstance(runs, list) or not all(isinstance(profile, Mapping) for profile in runs):
            runs = []
            complete = False
        profiles.append(tuple((profile.get("profile"), profile.get("training_input_hash")) for profile in runs))
        expected = selected[0].name.rsplit(":", 1)[-1].split(",") if selected else []
        reuse_failures = TrainingReuseFailures(initial, repeat, expected)
        sample_reuse = summary.get("sample_reuse")
        sample_failures = TrainingSampleReuseFailures(initial, sample_reuse, expected)
        sample_run = sample_reuse.get("run") if isinstance(sample_reuse, Mapping) else None
        complete = (
            complete
            and selected[0].return_code == 0
            and selected[0].payload is not None
            and selected[0].payload.get("status") == "passed"
            and bool(initial.get("complete"))
            and bool(repeat.get("complete"))
            and bool(summary.get("source_identity_unchanged"))
            and len(runs) == len(expected)
            and {profile.get("profile") for profile in runs} == set(expected)
            and all(profile.get("training_input_hash") for profile in runs)
            and (mode != "candidate" or not reuse_failures)
            and not sample_failures
        )
        values = _fingerprint_values(initial.get("numeric_fingerprints"), expected)
        complete = (
            complete
            and values is not None
            and values == _fingerprint_values(repeat.get("numeric_fingerprints"), expected)
        )
        fingerprints.append(values)
        initial_seconds, repeat_seconds = _seconds(initial.get("seconds")), _seconds(repeat.get("seconds"))
        timed = initial_seconds is not None and repeat_seconds is not None
        rounds.append(
            {
                "mode": mode,
                "complete": bool(complete and timed),
                "initial_seconds": initial_seconds if timed else None,
                "repeat_seconds": repeat_seconds if timed else None,
                "repeat_profiles": repeat.get("profiles"),
                "repeat_execution": repeat.get("execution"),
                "peak_rss_bytes": summary.get("process_peak_rss_bytes"),
                "reuse_failures": reuse_failures,
                "sample_reuse": sample_reuse,
                "sample_reuse_failures": sample_failures,
                "sample_reuse_seconds": _seconds(sample_run.get("seconds")) if isinstance(sample_run, Mapping) else None,
            }
        )
    comparable = (
        all(item["complete"] for item in rounds)
        and fingerprints[0] == fingerprints[1] == fingerprints[2]
        and profiles[0] == profiles[1] == profiles[2]
        and configuration_hashes[0] == configuration_hashes[1] == configuration_hashes[2]
    )
    ratios = {}
    for scenario in ("initial_seconds", "sample_reuse_seconds", "repeat_seconds"):
        old, current, old_after = (_seconds(item[scenario]) for item in rounds)
        ratios[scenario] = (
            current / min(old, old_after)
            if comparable and old is not None and current is not None and old_after is not None
            else None
        )
    return {
        "mode": "product_checkouts",
        "rounds": rounds,
        "comparable": comparable,
        "candidate_ratio_vs_best_baseline": ratios,
        "comparison_scope": "persisted_model_values_and_validation_metrics; no separate prediction replay",
        "production_authority": False,
        "concurrency_enablement_gate": False,
    }


def _seconds(value: object) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0:
        return float(value)
    return None


def _fingerprint_values(value: object, profiles: Sequence[str]) -> tuple[tuple[str, str, str], ...] | None:
    expected = {(profile, strategy) for profile in profiles for strategy in ("tomorrow", "d25")}
    if not isinstance(value, (tuple, list)) or len(value) != len(expected):
        return None
    parsed: dict[tuple[str, str], str] = {}
    for item in value:
        if not isinstance(item, Mapping):
            return None
        profile, strategy, digest = (
            item.get(key) for key in ("profile", "strategy", "model_values_and_metrics_sha256")
        )
        if not isinstance(profile, str) or not isinstance(strategy, str) or not isinstance(digest, str):
            return None
        key = (profile, strategy)
        if (
            key not in expected
            or key in parsed
            or len(digest) != 64
            or any(char not in "0123456789abcdef" for char in digest)
        ):
            return None
        parsed[key] = digest
    return tuple((profile, strategy, digest) for (profile, strategy), digest in sorted(parsed.items()))
