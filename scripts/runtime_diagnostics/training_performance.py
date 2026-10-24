"""Plan fresh, directly supervised training processes for comparable benchmark rounds."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

from .diagnostic_types import DiagnosticCommand, DiagnosticOptions, DiagnosticResult


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
    profiles = options.training_profiles
    commands = []
    for mode in ("serial_before", "parallel", "serial_after"):
        selections = (profiles,) if mode == "parallel" else tuple((profile,) for profile in profiles)
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
                    ),
                    options.command_timeout_seconds,
                )
            )
    return tuple(commands)


def SummarizeTrainingBenchmark(results: Sequence[DiagnosticResult]) -> dict[str, object]:
    modes = ("serial_before", "parallel", "serial_after")
    rounds = []
    identities = []
    for mode in modes:
        selected = tuple(result for result in results if result.name.startswith("training_alignment:" + mode + ":"))
        ready = bool(selected) and all(
            result.return_code == 0 and result.payload is not None and result.payload.get("status") == "passed"
            for result in selected
        )
        fingerprints = []
        peak = 0
        for result in selected:
            summary = result.payload.get("summary") if result.payload is not None else None
            if not isinstance(summary, Mapping):
                ready = False
                continue
            profiles = summary.get("profiles")
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
                "seconds": sum(result.duration_ms for result in selected) / 1000,
                "peak_rss_bytes": peak or None,
            }
        )
    comparable = (
        all(item["complete"] for item in rounds)
        and bool(identities[0])
        and identities[0] == identities[1] == identities[2]
    )
    parallel_seconds = float(rounds[1]["seconds"])
    speedup = (
        min(float(rounds[0]["seconds"]), float(rounds[2]["seconds"])) / parallel_seconds
        if comparable and parallel_seconds
        else None
    )
    return {
        "rounds": rounds,
        "comparable": comparable,
        "speedup_vs_best_serial": speedup,
        "production_authority": False,
        "concurrency_enablement_gate": False,
    }
