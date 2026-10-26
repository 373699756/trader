"""Plan fresh, directly supervised training processes for comparable benchmark rounds."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TypedDict

from .diagnostic_types import DiagnosticCommand, DiagnosticOptions, DiagnosticResult


class _Round(TypedDict):
    mode: str
    complete: bool
    seconds: float | None
    repeat_seconds: float | None
    peak_rss_bytes: int | None


class _CheckoutRound(TypedDict):
    mode: str
    complete: bool
    initial_seconds: float | None
    repeat_seconds: float | None
    repeat_profiles: object
    repeat_execution: object
    peak_rss_bytes: object


def BuildTrainingBenchmarkCommands(options: DiagnosticOptions, python_executable: str) -> tuple[DiagnosticCommand, ...]:
    if options.history_root is None or options.training_probe_directory is None:
        raise ValueError("training_benchmark_requires_history_and_output")
    project = Path(__file__).resolve().parents[2]
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
                        "scripts.runtime_diagnostics.training_alignment",
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
            }
        )
    comparable = (
        all(item["complete"] for item in rounds)
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
            }
        )
    comparable = (
        all(item["complete"] for item in rounds)
        and fingerprints[0] == fingerprints[1] == fingerprints[2]
        and profiles[0] == profiles[1] == profiles[2]
        and configuration_hashes[0] == configuration_hashes[1] == configuration_hashes[2]
    )
    ratios = {}
    for scenario in ("initial_seconds", "repeat_seconds"):
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
