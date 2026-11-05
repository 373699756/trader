"""Parse only the selected training action and validate its inputs."""

from __future__ import annotations

import argparse
import math
from collections.abc import Sequence
from datetime import date
from pathlib import Path

from scripts.core.paths import PROJECT_ROOT, ExternalPath
from scripts.training.options import TrainingOptions
from scripts.training.output_paths import RequireIndependentOutput
from scripts.training.resources.execution_selection import ParseExecutionComponents
from trader.recommendation.domain.scoring.profile_identity import SCORING_PROFILE_IDS
from trader.recommendation.domain.scoring.profile_selection import ParseProfileSelection


def BuildParser(
    action_identity: str, *, parents: Sequence[argparse.ArgumentParser] = ()
) -> argparse.ArgumentParser:
    if action_identity not in ("v5-training","v5-samples","training-samples","v5-lite","training-alignment","training-coverage","training-performance","training-resources","model-inputs",):
        raise ValueError("unsupported training action")
    parser = argparse.ArgumentParser(description=__doc__, parents=list(parents), allow_abbrev=False)
    if not parents:
        parser.add_argument("--command-timeout-seconds", type=float, default=180.0)
    parser.set_defaults(
        action_identity=action_identity,
        command_timeout_seconds=180.0,
        runtime_config=PROJECT_ROOT / 'config/runtime.json',
        history_root=None,
        v5_probe_directory=None,
        v5_kernel_probe=False,
        training_probe_directory=None,
        wide_sample_source=None,
        baseline_checkout=None,
        v5_sample_root=None,
        v5_candidate_config=None,
        training_profiles=SCORING_PROFILE_IDS,
        training_signal_date=None,
        product_checkout=None,
        published_history_read_only=False,
        sample_cache_transfer="copy",
        scheduling_candidate="baseline",
        scheduling_evidence=None,
        computation_candidate="baseline",
        execution_candidate="baseline",
        cpu_candidate="baseline",
        execution_components=(),
        fanout_candidate="baseline",
        training_benchmark_rounds=1,
        reference_config=None,
        quality_only=False,
        largest_only=False,
        cancel_after_batches=None,
        prepare_samples=False,
        prepare_only=False,
        training_sample_size=64,
        training_sample_sessions=750,
    )
    if action_identity in ("v5-training","v5-samples","training-samples","v5-lite","training-alignment","training-coverage","training-performance","model-inputs",):
        parser.add_argument('--runtime-config', default=str(PROJECT_ROOT / 'config/runtime.json'), help='runtime JSON configuration')
    if action_identity in ("v5-training","training-alignment","training-performance","model-inputs",):
        parser.add_argument('--history-root', type=Path, default=None, help='published history input; active history requires explicit read-only mode')
    if action_identity in ("v5-training",):
        parser.add_argument('--v5-probe-directory', type=Path, help='new external directory for isolated V5 training')
    if action_identity in ("v5-training",):
        parser.add_argument('--v5-kernel-probe', action='store_true', help='synthetic CPU kernel comparison; no history reads')
    if action_identity in ("v5-samples","training-samples","v5-lite","training-alignment","training-performance",):
        parser.add_argument('--training-probe-directory', type=Path, help='new external directory for all-profile training')
    if action_identity in ("v5-samples",):
        parser.add_argument('--wide-sample-source', type=Path, help='bounded read-only V5 wide-cache database comparison')
    if action_identity in ("training-samples","training-performance",):
        parser.add_argument('--baseline-checkout', type=Path, help='isolated checkout for history-write, download-performance or database-baseline comparisons')
    if action_identity in ("v5-lite",):
        parser.add_argument('--v5-sample-root', type=Path, help='sealed compact V5 cache for isolated lite comparison')
    if action_identity in ("v5-lite",):
        parser.add_argument('--v5-candidate-config', type=Path, help='explicit training JSON candidate for v5-lite')
    if action_identity in ("v5-lite",):
        parser.add_argument("--reference-config", type=Path)
    if action_identity in ("v5-lite",):
        parser.add_argument("--quality-only", action="store_true")
    if action_identity in ("v5-lite",):
        parser.add_argument("--largest-only", action="store_true")
    if action_identity in ("v5-lite",):
        parser.add_argument("--cancel-after-batches", type=int)
    if action_identity in ("v5-lite",):
        parser.add_argument("--prepare-samples", action="store_true")
    if action_identity in ("v5-lite",):
        parser.add_argument("--prepare-only", action="store_true")
    if action_identity in ("training-alignment","training-performance","training-resources",):
        parser.add_argument('--training-profiles', type=ParseProfileSelection, default=SCORING_PROFILE_IDS, help='comma-separated profiles for training-alignment and training-performance')
    if action_identity in ("training-alignment",):
        parser.add_argument("--product-checkout", type=Path, help="explicit frozen product checkout")
    if action_identity in ("training-alignment", "training-performance"):
        parser.add_argument("--published-history-read-only", action="store_true", help="fit with guarded read-only published history")
    if action_identity in ("training-alignment", "training-performance"):
        parser.add_argument("--sample-cache-transfer", choices=("copy", "hardlink"), default="copy")
    if action_identity in ("training-alignment",):
        parser.add_argument("--scheduling-candidate", choices=("baseline", "fixed", "long_task_first", "borrowing"), default="baseline")
    if action_identity in ("training-alignment",):
        parser.add_argument("--scheduling-evidence", type=Path)
    if action_identity in ("training-alignment", "training-performance"):
        parser.add_argument("--computation-candidate", choices=("baseline", "indexed_anchors", "schema_cache", "combined"), default="baseline")
    if action_identity in ("training-alignment", "training-performance"):
        parser.add_argument("--execution-candidate", choices=("baseline", "resources", "ram_matrices", "shared_validation", "normalized_tcn", "combined", "selected"), default="baseline")
        parser.add_argument("--cpu-candidate", choices=("baseline", "two_plus_one", "three_single"), default="baseline")
        parser.add_argument("--execution-components", type=ParseExecutionComponents, default=(), help="comma-separated components; required only for selected execution")
        parser.add_argument("--fanout-candidate", choices=("baseline", "batch_double", "queue_double"), default="baseline")
    if action_identity == "training-performance":
        parser.add_argument("--training-benchmark-rounds", type=int, default=1)
    if action_identity in ("training-coverage",):
        parser.add_argument('--training-signal-date', type=date.fromisoformat, help='read-only V5 label horizon probe')
    if action_identity == "training-samples":
        parser.add_argument('--size', dest='training_sample_size', type=int, default=64, help='sample comparison stock count within 4..128')
        parser.add_argument('--sessions', dest='training_sample_sessions', type=int, default=750, help='sample comparison sessions within 64..750')
    return parser


def ParseOptions(args: argparse.Namespace) -> TrainingOptions:
    options = TrainingOptions(
        action_identity=args.action_identity,
        command_timeout_seconds=args.command_timeout_seconds,
        runtime_config=Path(args.runtime_config).expanduser().resolve(),
        history_root=args.history_root.expanduser().resolve() if args.history_root is not None else None,
        v5_probe_directory=ExternalPath(args.v5_probe_directory, "--v5-probe-directory"),
        v5_kernel_probe=args.v5_kernel_probe,
        training_probe_directory=ExternalPath(args.training_probe_directory, "--training-probe-directory"),
        wide_sample_source=args.wide_sample_source.expanduser().resolve() if args.wide_sample_source is not None else None,
        baseline_checkout=ExternalPath(args.baseline_checkout, "--baseline-checkout"),
        v5_sample_root=args.v5_sample_root.expanduser().resolve() if args.v5_sample_root is not None else None,
        v5_candidate_config=args.v5_candidate_config.expanduser().resolve() if args.v5_candidate_config is not None else None,
        training_profiles=tuple(args.training_profiles),
        training_signal_date=args.training_signal_date,
        product_checkout=args.product_checkout.expanduser().resolve() if args.product_checkout is not None else None,
        published_history_read_only=args.published_history_read_only,
        sample_cache_transfer=args.sample_cache_transfer,
        scheduling_candidate=args.scheduling_candidate,
        scheduling_evidence=args.scheduling_evidence.expanduser().resolve() if args.scheduling_evidence is not None else None,
        computation_candidate=args.computation_candidate,
        execution_candidate=args.execution_candidate,
        cpu_candidate=args.cpu_candidate,
        execution_components=args.execution_components,
        fanout_candidate=args.fanout_candidate,
        training_benchmark_rounds=args.training_benchmark_rounds,
        reference_config=args.reference_config.expanduser().resolve() if args.reference_config is not None else None,
        quality_only=args.quality_only,
        largest_only=args.largest_only,
        cancel_after_batches=args.cancel_after_batches,
        prepare_samples=args.prepare_samples,
        prepare_only=args.prepare_only,
        training_sample_size=args.training_sample_size,
        training_sample_sessions=args.training_sample_sessions,
    )
    if not math.isfinite(options.command_timeout_seconds) or not 0 < options.command_timeout_seconds <= 7200:
        raise ValueError("command timeout must be positive and must not exceed 7200 seconds")
    if options.training_probe_directory is not None:
        for checkout in (options.baseline_checkout, options.product_checkout):
            if checkout is not None:
                RequireIndependentOutput(options.training_probe_directory, checkout)
    if options.action_identity == "v5-training":
        if options.v5_kernel_probe and options.v5_probe_directory is not None:
            raise ValueError("V5 kernel and real training probes are mutually exclusive")
        if not options.v5_kernel_probe and options.v5_probe_directory is None:
            raise ValueError("v5-training requires --v5-probe-directory")
    if options.action_identity in {"v5-samples", "v5-lite", "training-samples"} and options.training_probe_directory is None:
        raise ValueError(f"{options.action_identity} requires --training-probe-directory")
    if options.action_identity == "training-samples" and options.baseline_checkout is None:
        raise ValueError("training-samples requires --baseline-checkout")
    if options.action_identity in {"training-alignment", "training-performance"} and (
        options.training_probe_directory is None or options.history_root is None
    ):
        raise ValueError(f"{options.action_identity} requires --history-root and --training-probe-directory")
    if options.action_identity == "v5-lite":
        if options.v5_sample_root is not None and options.v5_candidate_config is None:
            raise ValueError("--v5-sample-root requires --v5-candidate-config")
        if options.largest_only and (options.quality_only or options.reference_config is not None or options.prepare_only):
            raise ValueError("V5 largest-only cannot run a quality comparison or prepare-only")
        if options.cancel_after_batches is not None and (not options.largest_only or options.cancel_after_batches < 1):
            raise ValueError("V5 cancellation requires largest-only and a positive batch count")
        if options.prepare_only and not options.prepare_samples:
            raise ValueError("V5 prepare-only requires explicit prepare-samples")
        if options.prepare_samples and options.v5_sample_root is None:
            raise ValueError("V5 sample preparation requires an explicit sample root")
        if (options.quality_only or options.largest_only) and (options.v5_sample_root is None or options.v5_candidate_config is None):
            raise ValueError("V5 quality-only/largest-only requires samples and a candidate configuration")
    if options.action_identity == "training-alignment":
        if options.scheduling_candidate != "baseline" and options.scheduling_evidence is None:
            raise ValueError("training scheduling candidates require --scheduling-evidence")
        if options.scheduling_candidate == "baseline" and options.scheduling_evidence is not None:
            raise ValueError("--scheduling-evidence requires an explicit scheduling candidate")
    if options.action_identity == "training-performance":
        if not 1 <= options.training_benchmark_rounds <= 5:
            raise ValueError("--training-benchmark-rounds must be within 1..5")
        if options.baseline_checkout is None and (
            options.execution_candidate != "baseline"
            or options.cpu_candidate != "baseline"
            or options.fanout_candidate != "baseline"
            or options.computation_candidate != "baseline"
            or options.training_benchmark_rounds > 1
        ):
            raise ValueError("training performance candidates or repeated rounds require --baseline-checkout")
    if not 4 <= options.training_sample_size <= 128 or not 64 <= options.training_sample_sessions <= 750:
        raise ValueError("sample comparison requires size within 4..128 and sessions within 64..750")
    return options
