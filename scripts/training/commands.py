"""Build only the selected training invocation."""

from __future__ import annotations

import sys
from dataclasses import replace

from scripts.core.catalog import WorkerArguments
from scripts.core.contracts import DiagnosticCommand
from scripts.training.options import TrainingOptions


def BuildCommands(
    options: TrainingOptions, *, python_executable: str = sys.executable
) -> tuple[DiagnosticCommand, ...]:
    common_timeout = options.command_timeout_seconds
    commands: tuple[DiagnosticCommand, ...]
    if options.action_identity == "training-performance":
        from scripts.training.jobs.training_performance import BuildTrainingBenchmarkCommands

        commands = BuildTrainingBenchmarkCommands(options, python_executable)
        return tuple(replace(command, argv=WorkerArguments(command.argv)) for command in commands)
    if options.action_identity in {"v5-training", "model-inputs"} and not options.v5_kernel_probe and options.history_root is None:
        from trader.infra.settings import LoadRuntimeSettings

        options = replace(options, history_root=LoadRuntimeSettings(options.runtime_config).data_paths.history)
    if options.action_identity == "v5-training":
        commands = (DiagnosticCommand(
            "v5_training",
            (
                python_executable,
                "-m",
                "scripts.training.models.v5_training",
                "--runtime-config", str(options.runtime_config),
                *(
                    ("--kernel-probe",)
                    if options.v5_kernel_probe
                    else ("--history-root", str(options.history_root), "--work-root", str(options.v5_probe_directory))
                ),
            ),
            common_timeout,
        ),)
    elif options.action_identity == "v5-samples":
        commands = (DiagnosticCommand(
            "v5_samples",
            (
                python_executable,
                "-m",
                "scripts.training.samples.v5_samples",
                "--work-root",
                str(options.training_probe_directory),
                "--config",
                str(options.runtime_config),
            )
            + (("--wide-sample-source", str(options.wide_sample_source)) if options.wide_sample_source else ()),
            common_timeout,
        ),)
    elif options.action_identity == "training-samples":
        commands = (DiagnosticCommand(
            "training_samples",
            (python_executable, "-m", "scripts.training.samples.training_samples",
             "--work-root", str(options.training_probe_directory), "--baseline-checkout", str(options.baseline_checkout),
             "--config", str(options.runtime_config),
             "--size", str(options.training_sample_size), "--sessions", str(options.training_sample_sessions)),
            common_timeout,
        ),)
    elif options.action_identity == "v5-lite":
        commands = (DiagnosticCommand(
            "v5_lite",
            (
                python_executable,
                "-m",
                "scripts.training.models.v5_lite",
                "--work-root",
                str(options.training_probe_directory),
                "--runtime-config", str(options.runtime_config),
            )
            + (("--candidate-config", str(options.v5_candidate_config)) if options.v5_candidate_config else ())
            + (("--sample-root", str(options.v5_sample_root)) if options.v5_sample_root else ())
            + (("--reference-config", str(options.reference_config)) if options.reference_config else ())
            + (("--quality-only",) if options.quality_only else ())
            + (("--largest-only",) if options.largest_only else ())
            + (("--cancel-after-batches", str(options.cancel_after_batches)) if options.cancel_after_batches is not None else ())
            + (("--prepare-samples",) if options.prepare_samples else ())
            + (("--prepare-only",) if options.prepare_only else ()),
            common_timeout,
        ),)
    elif options.action_identity == "training-alignment":
        commands = (DiagnosticCommand(
            "training_alignment",
            (
                python_executable,
                "-m",
                "scripts.training.jobs.training_alignment",
                "--history-root",
                str(options.history_root),
                "--work-root",
                str(options.training_probe_directory),
                "--profiles",
                ",".join(options.training_profiles),
                "--runtime-config", str(options.runtime_config),
                "--sample-cache-transfer", options.sample_cache_transfer,
                "--scheduling-candidate", options.scheduling_candidate,
                "--computation-candidate", options.computation_candidate,
            )
            + (("--product-checkout", str(options.product_checkout)) if options.product_checkout is not None else ())
            + (("--published-history-read-only",) if options.published_history_read_only else ())
            + (("--execution-candidate", options.execution_candidate) if options.execution_candidate != "baseline" else ())
            + (("--cpu-candidate", options.cpu_candidate) if options.cpu_candidate != "baseline" else ())
            + (("--execution-components", ",".join(options.execution_components)) if options.execution_components else ())
            + (("--fanout-candidate", options.fanout_candidate) if options.fanout_candidate != "baseline" else ())
            + (("--scheduling-evidence", str(options.scheduling_evidence)) if options.scheduling_evidence is not None else ()),
            common_timeout,
        ),)
    elif options.action_identity == "training-coverage":
        commands = (DiagnosticCommand(
            "training_coverage",
            (
                python_executable,
                "-m",
                "scripts.training.samples.training_coverage",
                "--config",
                str(options.runtime_config),
            )
            + (("--signal-date", options.training_signal_date.isoformat()) if options.training_signal_date else ()),
            common_timeout,
        ),)
    elif options.action_identity == "training-resources":
        commands = (DiagnosticCommand(
            "training_resources",
            (
                python_executable, "-m", "scripts.training.resources.training_resources",
                "--profiles", ",".join(options.training_profiles),
            ),
            min(common_timeout, 30.0),
        ),)
    elif options.action_identity == "model-inputs":
        commands = (DiagnosticCommand(
            "model_inputs",
            (
                python_executable,
                "-m",
                "scripts.training.jobs.training_alignment",
                "--history-root",
                str(options.history_root),
                "--inputs-only",
                "--runtime-config",
                str(options.runtime_config),
            ),
            common_timeout,
        ),)
    else:
        raise ValueError("unsupported training action")
    return tuple(replace(command, argv=WorkerArguments(command.argv)) for command in commands)
