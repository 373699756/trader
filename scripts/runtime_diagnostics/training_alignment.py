"""Train all alignment contracts against an explicitly external frozen history copy."""

from __future__ import annotations

import argparse
import io
import json
import sys
import time
from collections import Counter
from contextlib import redirect_stdout
from datetime import datetime
from datetime import time as daytime
from pathlib import Path
from zoneinfo import ZoneInfo

from trader.bootstrap import ExecuteSelectedTraining
from trader.download.application.read_published_history import ReadPublishedHistoryUseCase
from trader.download.infra.published_history_snapshot import SQLitePublishedHistorySnapshot
from trader.infra.settings import LoadRuntimeSettings
from trader.recommendation.domain.market.history_alignment import HISTORY_ALIGNMENT_CONTRACT
from trader.recommendation.domain.scoring.profile_identity import SCORING_PROFILE_IDS, ScoringProfileId
from trader.recommendation.domain.scoring.profile_selection import ParseProfileSelection
from trader.recommendation.infra.market_data.published_history_cache import PublishedHistoryCache
from trader.recommendation.infra.scoring.profile_factory import LoadScoringProfile
from trader.training.entrypoints.commands import ConfigureTrainingResources
from trader.training.infra.history.history_training_input import SQLiteHistoryTrainingInputSnapshot

from .reporting import emit_report


def _inputs(history: Path) -> dict[str, object]:
    started = time.monotonic()
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
            "source_active_writes": 0,
            "network_calls": 0,
            "model_predictions": 0,
        },
    }


def _probe(
    history: Path, work: Path, profiles: tuple[ScoringProfileId, ...] = SCORING_PROFILE_IDS
) -> dict[str, object]:
    project = Path(__file__).resolve().parents[2]
    history, work = history.resolve(), work.resolve()
    if history.is_relative_to(project) or work.is_relative_to(project) or work == history:
        raise ValueError("training_alignment_requires_external_history_copy")
    if work.exists() or history.is_relative_to(work) or work.is_relative_to(history):
        raise ValueError("training_alignment_requires_new_separate_output")
    source = SQLiteHistoryTrainingInputSnapshot.open(history)
    identity = source.snapshot.active_snapshot_hash
    work.mkdir(parents=True)
    started = time.monotonic()
    runtime = LoadRuntimeSettings(project / "config/runtime.json")
    ConfigureTrainingResources()
    captured = io.StringIO()
    with redirect_stdout(captured):
        ExecuteSelectedTraining(runtime, profiles, history_root=history, training_root=work)
    reports = [json.loads(line) for line in captured.getvalue().splitlines() if line.startswith("{")]
    results = [item for item in reports if item.get("schema_version") == "profile_training_result"]
    reloaded_heads = sum(
        len(LoadScoringProfile(item["profile"], training_root=work / "models").heads)
        for item in results
        if item["status"] == "engineering_ready"
    )
    unchanged = SQLiteHistoryTrainingInputSnapshot.open(history).snapshot.active_snapshot_hash == identity
    return {
        "schema_version": "training-alignment-diagnostic",
        "status": "passed" if reloaded_heads == len(profiles) * 2 and unchanged else "degraded",
        "summary": {
            "alignment_contract": HISTORY_ALIGNMENT_CONTRACT,
            "profiles": results,
            "execution": next(
                (item for item in reports if item.get("schema_version") == "selected_training_result"), None
            ),
            "source_codes": len(source.snapshot.training_codes),
            "source_identity_unchanged": unchanged,
            "reloaded_heads": reloaded_heads,
            "process_peak_rss_bytes": _peak_rss_bytes(),
            "seconds": round(time.monotonic() - started, 3),
            "settings": "configured_unmodified",
            "production_authority": False,
            "return_validation": "not_evaluated",
        },
    }


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
    parser.add_argument("--profiles", type=ParseProfileSelection, default=SCORING_PROFILE_IDS)
    parser.add_argument("--benchmark-mode", choices=("serial_before", "parallel", "serial_after"))
    args = parser.parse_args()
    try:
        if args.inputs_only:
            report = _inputs(args.history_root)
        elif args.work_root is not None:
            report = _probe(args.history_root, args.work_root, args.profiles)
            report["benchmark_mode"] = args.benchmark_mode
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
