"""Train all alignment contracts against an explicitly external frozen history copy."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import sys
import time
from collections import Counter
from contextlib import redirect_stdout
from datetime import datetime
from datetime import time as daytime
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from .reporting import emit_report

if TYPE_CHECKING:
    from trader.infra.settings import RuntimeSettings
    from trader.recommendation.domain.scoring.profile_identity import ScoringProfileId


def _inputs(history: Path, runtime_config: Path) -> dict[str, object]:
    from trader.download.application.read_published_history import ReadPublishedHistoryUseCase
    from trader.download.infra.published_history_snapshot import SQLitePublishedHistorySnapshot
    from trader.infra.settings import LoadRuntimeSettings
    from trader.recommendation.infra.market_data.published_history_cache import PublishedHistoryCache
    from trader.recommendation.infra.market_data.shared_history_projection import SharedHistoryProjection

    started = time.monotonic()
    settings = LoadRuntimeSettings(runtime_config)
    source = ReadPublishedHistoryUseCase(SQLitePublishedHistorySnapshot(history))
    manifest = source.manifest()
    if manifest is None:
        raise ValueError("history_manifest_unavailable")
    cache = PublishedHistoryCache(
        source,
        lookback_sessions=251,
        monotonic=time.monotonic,
        open_dates=lambda: manifest.calendar_dates,
        learned_rank_inputs=True,
        projection_owner=SharedHistoryProjection(
            source, monotonic=time.monotonic, settings=settings.pipeline.history_preload,
        ),
    )
    cache.initialize(cancelled=lambda: False)
    as_of = datetime.combine(manifest.data_cutoff, daytime(20, 0), ZoneInfo("Asia/Shanghai"))
    inputs = cache.completed_model_inputs(manifest.universe_codes, as_of)
    features = {item.code: item.learned_rank_values for item in inputs if item.learned_rank_values}
    reasons = Counter(item.quality.failure or "ready" for item in inputs)
    status = cache.status()
    return {
        "schema_version": "model-inputs-diagnostic",
        "status": "passed" if features else "degraded",
        "summary": {
            "profile": "v1",
            "source_codes": len(manifest.universe_codes),
            "input_ready_codes": len(features),
            "processed_codes": status.maintenance_completed_units,
            "history_scan_count": status.history_scan_count,
            "history_window_count": status.history_window_count,
            "history_quality": dict(reasons),
            "estimated_history_codes": status.estimated_history_codes,
            "suspended_history_codes": status.suspended_history_codes,
            "failure": status.maintenance_reason,
            "seconds": round(time.monotonic() - started, 3),
            "source_identity": manifest.snapshot_hash,
            "signal_date": manifest.data_cutoff.isoformat(),
            "source_data_writes": 0,
            "network_calls": 0,
            "model_predictions": 0,
        },
    }


def _probe(
    history: Path,
    work: Path,
    profiles: tuple[ScoringProfileId, ...] | None = None,
    *,
    runtime_config: Path | None = None,
) -> dict[str, object]:
    from trader.infra.settings import LoadRuntimeSettings
    from trader.recommendation.domain.market.history_alignment import HISTORY_ALIGNMENT_CONTRACT
    from trader.recommendation.domain.scoring.profile_identity import SCORING_PROFILE_IDS
    from trader.training.entrypoints.commands import ConfigureTrainingResources
    from trader.training.infra.history.history_training_input import SQLiteHistoryTrainingInputSnapshot
    from trader.training.infra.implementation_identity import TrainingImplementationIdentity

    project = Path(__file__).resolve().parents[2]
    history, work = history.resolve(), work.resolve()
    if history.is_relative_to(project) or work.is_relative_to(project) or work == history:
        raise ValueError("training_alignment_requires_external_history_copy")
    if work.exists() or history.is_relative_to(work) or work.is_relative_to(history):
        raise ValueError("training_alignment_requires_new_separate_output")
    source = SQLiteHistoryTrainingInputSnapshot.open(history)
    identity = source.snapshot.snapshot_hash
    work.mkdir(parents=True)
    started = time.monotonic()
    profiles = SCORING_PROFILE_IDS if profiles is None else profiles
    config = (runtime_config or project / "config/runtime.json").resolve()
    runtime = LoadRuntimeSettings(config)
    configuration_hash = hashlib.sha256(
        b"".join(
            hashlib.sha256((runtime.project_root / "config" / name).read_bytes()).digest()
            for name in ("training.json", "features.json")
        )
    ).hexdigest()
    ConfigureTrainingResources()
    initial = _train_once(runtime, history, work, profiles)
    repeat = _train_once(runtime, history, work, profiles) if initial["complete"] else None
    unchanged = SQLiteHistoryTrainingInputSnapshot.open(history).snapshot.snapshot_hash == identity
    complete = bool(initial["complete"] and repeat is not None and repeat["complete"] and unchanged)
    equal = repeat is not None and initial["numeric_fingerprints"] == repeat["numeric_fingerprints"]
    return {
        "schema_version": "training-alignment-diagnostic",
        "status": "passed" if complete and equal else "degraded",
        "summary": {
            "alignment_contract": HISTORY_ALIGNMENT_CONTRACT,
            "profiles": initial["profiles"],
            "execution": initial["execution"],
            "initial": initial,
            "repeat": repeat,
            "repeat_model_values_equal": equal,
            "source_codes": len(source.snapshot.training_codes),
            "source_identity_unchanged": unchanged,
            "reloaded_heads": initial["reloaded_heads"],
            "process_peak_rss_bytes": _peak_rss_bytes(),
            "seconds": initial["seconds"],
            "total_seconds": round(time.monotonic() - started, 6),
            "settings": "configured_unmodified",
            "training_configuration_sha256": configuration_hash,
            "training_implementation_sha256": TrainingImplementationIdentity(),
            "production_authority": False,
            "return_validation": "not_evaluated",
            "prediction_comparison": "persisted_model_values_and_validation_metrics; no separate input replay",
        },
    }


def _train_once(
    runtime: RuntimeSettings, history: Path, work: Path, profiles: tuple[ScoringProfileId, ...]
) -> dict[str, object]:
    from trader.bootstrap import ExecuteSelectedTraining
    from trader.recommendation.infra.scoring.profile_factory import LoadScoringProfile

    captured = io.StringIO()
    started = time.monotonic()
    with redirect_stdout(captured):
        code = ExecuteSelectedTraining(runtime, profiles, history_root=history, training_root=work)
    seconds = time.monotonic() - started
    reports = [json.loads(line) for line in captured.getvalue().splitlines() if line.startswith("{")]
    results = [item for item in reports if item.get("schema_version") == "profile_training_result"]
    reloaded_heads = sum(
        len(LoadScoringProfile(item["profile"], training_root=work / "models").heads)
        for item in results
        if item["status"] == "engineering_ready"
    )
    complete = code == 0 and reloaded_heads == len(profiles) * 2 and len(results) == len(profiles)
    return {
        "complete": complete,
        "exit_code": code,
        "profiles": results,
        "execution": next((item for item in reports if item.get("schema_version") == "selected_training_result"), None),
        "reloaded_heads": reloaded_heads,
        "seconds": seconds,
        "numeric_fingerprints": _numeric_fingerprints(work / "models", profiles) if complete else (),
    }


def _numeric_fingerprints(model_root: Path, profiles: tuple[ScoringProfileId, ...]) -> tuple[dict[str, object], ...]:
    fingerprints: list[dict[str, object]] = []
    metric_fields = (
        "prediction_metrics",
        "industry_prediction_metrics",
        "calibration_metrics",
        "rejected_industries",
        "evaluation_coverage",
        "target_metrics",
        "training_rows",
        "validation_rows",
    )
    for profile in profiles:
        for strategy in ("tomorrow", "d25"):
            directory = model_root / profile / strategy
            documents = []
            for name in ("model.json", "metadata.json"):
                path = directory / name
                if path.stat().st_size > 32 * 1024**2:
                    raise ValueError("training_alignment_model_document_too_large")
                value = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(value, dict):
                    raise ValueError("training_alignment_model_document_invalid")
                documents.append(value)
            model, metadata = documents
            report = metadata["validation_report"]
            if not isinstance(report, dict):
                raise ValueError("training_alignment_validation_report_invalid")
            components = model["components"] if profile == "v1" else model["industries"]
            metrics = report["validation"] if profile == "v1" else {field: report[field] for field in metric_fields}
            numeric = json.dumps((components, metrics), ensure_ascii=True, sort_keys=True, allow_nan=False).encode()
            fingerprints.append(
                {
                    "profile": profile,
                    "strategy": strategy,
                    "model_values_and_metrics_sha256": hashlib.sha256(numeric).hexdigest(),
                }
            )
    return tuple(fingerprints)


def _select_product(checkout: Path | None) -> Path:
    project = Path(__file__).resolve().parents[2]
    expected = (checkout or project).resolve(strict=True)
    if not (expected / "src/trader").is_dir():
        raise ValueError("training_alignment_product_checkout_invalid")
    if checkout is not None:
        sys.path.insert(0, str(expected / "src"))
    import trader

    if trader.__file__ is None or Path(trader.__file__).resolve().parents[2] != expected:
        raise ValueError("training_alignment_product_import_mismatch")
    return expected


def _peak_rss_bytes() -> int | None:
    try:
        import resource

        value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return int(value if sys.platform == "darwin" else value * 1024)
    except ImportError:
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history-root", type=Path, required=True)
    parser.add_argument("--work-root", type=Path)
    parser.add_argument("--inputs-only", action="store_true")
    parser.add_argument("--profiles")
    parser.add_argument("--runtime-config", type=Path)
    parser.add_argument("--product-checkout", type=Path)
    parser.add_argument(
        "--benchmark-mode",
        choices=("serial_before", "parallel", "serial_after", "baseline_before", "candidate", "baseline_after"),
    )
    args = parser.parse_args()
    try:
        product = _select_product(args.product_checkout)
        from trader.recommendation.domain.scoring.profile_identity import SCORING_PROFILE_IDS
        from trader.recommendation.domain.scoring.profile_selection import ParseProfileSelection

        profiles = ParseProfileSelection(args.profiles) if args.profiles is not None else SCORING_PROFILE_IDS
        if args.inputs_only:
            report = _inputs(args.history_root, args.runtime_config or product / "config/runtime.json")
        elif args.work_root is not None:
            report = _probe(args.history_root, args.work_root, profiles, runtime_config=args.runtime_config)
            report["benchmark_mode"] = args.benchmark_mode
            report["product_checkout"] = str(product)
        else:
            raise ValueError("training_alignment_requires_output")
    except (OSError, RuntimeError, ValueError) as exc:
        report = {
            "schema_version": "training-alignment-diagnostic",
            "status": "failed",
            "summary": {"failure": type(exc).__name__},
        }
    emit_report(report)
    return int(report["status"] == "failed")


if __name__ == "__main__":
    raise SystemExit(main())
