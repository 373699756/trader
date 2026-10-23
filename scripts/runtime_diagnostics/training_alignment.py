"""Train all alignment contracts against an explicitly external frozen history copy."""

from __future__ import annotations

import argparse
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from datetime import time as daytime
from pathlib import Path
from zoneinfo import ZoneInfo

from trader.download.application.read_published_history import ReadPublishedHistoryUseCase
from trader.download.infra.published_history_snapshot import SQLitePublishedHistorySnapshot
from trader.recommendation.domain.market.history_alignment import HISTORY_ALIGNMENT_CONTRACT
from trader.recommendation.infra.market_data.published_history_cache import PublishedHistoryCache
from trader.recommendation.infra.scoring.profile_factory import LoadScoringProfile
from trader.training.application.tomorrow_training import TomorrowTrainingProgress, TrainingSampleDiagnostics
from trader.training.infra.engine import ProfileTrainingRequest, RunProfileTraining
from trader.training.infra.history.history_training_input import SQLiteHistoryTrainingInputSnapshot
from trader.training.infra.profile.v1.settings import LoadV1TrainingSettings
from trader.training.infra.profile.v1.training import RunV1Training
from trader.training.infra.profile.v2.contracts import V2_TRAINING_PROFILE
from trader.training.infra.profile.v3.contracts import V3_TRAINING_PROFILE
from trader.training.infra.profile.v5.contracts import V5_TRAINING_PROFILE
from trader.training.infra.training_settings import LoadProfileTrainingSettings

from .reporting import emit_report


@dataclass
class _Progress:
    last_stage: str | None = None
    rows: int = 0
    diagnostics: TrainingSampleDiagnostics | None = None

    def publish(self, progress: TomorrowTrainingProgress) -> None:
        self.last_stage = progress.stage
        if progress.stage == "history_conversion":
            self.rows = progress.completed_units
        if progress.sample_diagnostics is not None:
            self.diagnostics = progress.sample_diagnostics


def _progress_report(progress: _Progress) -> dict[str, object]:
    d = progress.diagnostics
    return {
        "last_stage": progress.last_stage,
        "source_rows": progress.rows,
        "missing_prices": d.missing_prices if d else None,
        "short_window": d.short_window if d else None,
        "estimated_sessions": d.estimated_sessions if d else None,
        "suspended_sessions": d.suspended_sessions if d else None,
        "missing_t1": d.missing_t1 if d else None,
        "missing_d25": d.missing_d25 if d else None,
    }


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
    features = cache.completed_model_features(manifest.universe_codes, as_of)
    reasons = Counter(cache.model_history_quality(code, as_of).failure or "ready" for code in manifest.universe_codes)
    status = cache.status()
    return {
        "schema_version": "model-inputs-diagnostic",
        "status": "passed" if features else "degraded",
        "summary": {
            "profile": "v1",
            "source_codes": len(manifest.universe_codes),
            "input_ready_codes": len(features),
            "processed_codes": status.maintenance_completed_units,
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


def _probe(history: Path, work: Path) -> dict[str, object]:
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
    results: list[dict[str, object]] = []
    v1 = RunV1Training(
        lambda: SQLiteHistoryTrainingInputSnapshot.open(history),
        work / "models",
        sample_root=work / "samples",
        history_root=history,
        settings=LoadV1TrainingSettings(project),
    )
    results.append(
        {
            "profile": "v1",
            "status": v1.status,
            "heads": len(v1.heads),
            "failures": list(v1.failure_reasons),
            "estimated_sessions": v1.sample_counts.estimated_sessions if v1.sample_counts else None,
            "suspended_sessions": v1.sample_counts.suspended_sessions if v1.sample_counts else None,
            "validation": [
                {
                    "strategy": h.strategy.value,
                    "predicted_rows": h.validation.predicted_rows,
                    "rank_ic": h.validation.mean_daily_rank_ic,
                    "ndcg_at_6": h.validation.mean_daily_ndcg_at_6,
                }
                for h in v1.heads
            ],
        }
    )
    reloaded_heads = 0
    if v1.status == "engineering_ready":
        reloaded_heads += len(LoadScoringProfile("v1", training_root=work / "models").heads)
    for profile in (V2_TRAINING_PROFILE, V3_TRAINING_PROFILE, V5_TRAINING_PROFILE):
        if profile.profile_id == "v5":
            try:
                from trader.training.infra.profile.v5.network import ConfigureTcnRuntime
            except ImportError:
                results.append({"profile": "v5", "status": "failed", "failures": ["v5_dependency_unavailable"]})
                continue
            ConfigureTcnRuntime(training=True)
        progress = _Progress()
        result = RunProfileTraining(
            ProfileTrainingRequest(
                history,
                work / "models",
                profile,
                profile.heads,
                LoadProfileTrainingSettings(project, profile.profile_id),
                sample_root=work / "samples",
                progress=progress,
            )
        )
        results.append(
            {
                "profile": profile.profile_id,
                "status": result.status,
                "samples": _progress_report(progress),
                "heads": [
                    {
                        "strategy": h.strategy.value,
                        "status": h.status,
                        "training_rows": h.training_rows,
                        "validation_rows": h.validation_rows,
                        "failures": list(h.failure_reasons),
                    }
                    for h in result.heads
                ],
            }
        )
        if result.status == "engineering_ready":
            reloaded_heads += len(LoadScoringProfile(profile.profile_id, training_root=work / "models").heads)
    unchanged = SQLiteHistoryTrainingInputSnapshot.open(history).snapshot.active_snapshot_hash == identity
    return {
        "schema_version": "training-alignment-diagnostic",
        "status": "passed" if reloaded_heads == 8 and unchanged else "degraded",
        "summary": {
            "alignment_contract": HISTORY_ALIGNMENT_CONTRACT,
            "profiles": results,
            "source_codes": len(source.snapshot.training_codes),
            "source_identity_unchanged": unchanged,
            "reloaded_heads": reloaded_heads,
            "seconds": round(time.monotonic() - started, 3),
            "settings": "configured_unmodified",
            "production_authority": False,
            "return_validation": "not_evaluated",
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history-root", type=Path, required=True)
    parser.add_argument("--work-root", type=Path)
    parser.add_argument("--inputs-only", action="store_true")
    args = parser.parse_args()
    try:
        if args.inputs_only:
            report = _inputs(args.history_root)
        elif args.work_root is not None:
            report = _probe(args.history_root, args.work_root)
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
