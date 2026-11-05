"""Retain explicit V5 quality inputs under samples without fitting or publishing models."""

from __future__ import annotations

import shutil
import math
import sys
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from scripts.training.jobs.training_source_guard import ReadOnlyTrainingSource
from trader.download.infra.history_control_index import HistoryMaintenanceLock
from trader.infra.atomic_files.json import AtomicWriteJson
from trader.infra.serialization.canonical_codec import CanonicalDigest
from trader.infra.settings import RuntimeSettings
from trader.infra.sqlite_policy import SqliteIoPolicy
from trader.training.application.tomorrow_training import TomorrowTrainingProgress, TomorrowTrainingWindow
from trader.training.application.resources import TrainingSampleSettings
from trader.training.domain.training_dates import BuildTrainingDateSplit
from trader.training.infra.history.aligned_training_rows import AlignmentReadDates, AlignTrainingRows
from trader.training.infra.history.history_training_input import SQLiteHistoryTrainingInputSnapshot
from trader.training.infra.implementation_identity import TrainingImplementationIdentity
from trader.training.infra.profile.v5.contracts import V5_TRAINING_PROFILE
from trader.training.infra.sample_builder import BuildTrainingSamples, TrainingSampleBuildRequest
from trader.training.infra.sample_cache import TrainingSampleCache
from trader.training.infra.sample_schema import TrainingFeatureNames
from trader.training.infra.sample_sequences import SampleSequenceLayout
from trader.training.infra.training_settings import LoadProfileTrainingSettings


@dataclass(frozen=True)
class V5QualitySamples:
    root: Path
    snapshot_hash: str
    source_identity_hash: str
    input_descriptor_hash: str
    source_cutoff: str
    split_hash: str
    sample_identity: str
    rows: int
    source_codes: int
    fit_dates: int
    reused: bool


class _Progress:
    def __init__(self, output: Path, deadline: float) -> None:
        self.output = output
        self.deadline = deadline
        self.reported = 0.0

    def publish(self, update: TomorrowTrainingProgress) -> None:
        now = time.monotonic()
        if now >= self.deadline:
            raise TimeoutError("v5_quality_sample_budget_exhausted")
        if now - self.reported < 10 and update.state != "completed":
            return
        value = {
            "phase": update.stage,
            "state": update.state,
            "completed": update.completed_units,
            "total": update.total_units,
        }
        AtomicWriteJson(self.output / "sample-progress.json", value, indent=2)
        print(
            f"V5质量样本 | {update.stage} | {update.completed_units}/{update.total_units}",
            file=sys.stderr,
            flush=True,
        )
        self.reported = now


def PrepareV5QualitySamples(
    runtime: RuntimeSettings,
    project: Path,
    root: Path,
    report_root: Path,
    deadline: float,
    *,
    sqlite_policy: SqliteIoPolicy,
    sample_settings: TrainingSampleSettings,
) -> V5QualitySamples:
    """Build or audit an explicitly owned cache; keep history and existing caches intact."""
    if not math.isfinite(deadline) or deadline <= time.monotonic():
        raise TimeoutError("v5_quality_sample_budget_exhausted")
    parent = runtime.data_paths.samples.resolve()
    if root.is_symlink() or not root.resolve().is_relative_to(parent) or root.resolve() == parent:
        raise ValueError("v5_quality_requires_a_child_of_configured_samples")
    root = root.resolve()
    existed = root.exists()
    settings = LoadProfileTrainingSettings(project, "v5")
    progress = _Progress(report_root, deadline)
    history_root = runtime.data_paths.history
    with (
        HistoryMaintenanceLock(history_root.with_name(".history.lock")),
        HistoryMaintenanceLock(runtime.data_paths.models / ".v5.training.lock"),
        ReadOnlyTrainingSource(history_root),
    ):
        history = SQLiteHistoryTrainingInputSnapshot.open(history_root, sqlite_policy=sqlite_policy)
        snapshot = history.snapshot
        split = BuildTrainingDateSplit(
            snapshot.calendar.open_dates,
            parent_manifest_hash=snapshot.snapshot_hash,
            fit_dates=settings.fit_dates,
            recent_fit=True,
            early_stopping_sessions=20,
            history_sessions=V5_TRAINING_PROFILE.history_sessions,
            history_excludes_signal=True,
        )
        identity = CanonicalDigest((
            "v5-quality-samples", TrainingImplementationIdentity(), snapshot.snapshot_hash,
            snapshot.input_descriptor_hash, settings, V5_TRAINING_PROFILE,
        ))
        cache = TrainingSampleCache(
            root,
            identity,
            TrainingFeatureNames(V5_TRAINING_PROFILE.heads),
            split.content_hash,
            checkpoint=lambda: progress.publish(TomorrowTrainingProgress("sample_cache_seal", "running", 0, 1)),
            sequence_layout=SampleSequenceLayout(6, 60, 6),
            sqlite_policy=sqlite_policy,
            sample_settings=sample_settings,
        )
        reused = cache.inspect()
        if existed and (not reused or not cache.audit()):
            raise ValueError("v5_quality_existing_sample_identity_or_digest_invalid")
        window = TomorrowTrainingWindow(split)
        if not reused:
            estimate = history.training_row_upper_bound(window.readable_dates) * 256 + 1024**3
            existing = root.parent
            while not existing.exists():
                existing = existing.parent
            if shutil.disk_usage(existing).free < estimate:
                raise ValueError("v5_quality_sample_space_insufficient")
        with cache.open(inspected=True) as samples:
            if not reused:
                dates = AlignmentReadDates(snapshot.calendar.open_dates, window.readable_dates)
                physical_total = history.training_row_upper_bound(dates)

                def report_read(completed: int) -> None:
                    progress.publish(TomorrowTrainingProgress(
                        "input_read", "running", min(completed, physical_total), physical_total,
                    ))

                with (
                    closing(history.iter_training_rows(dates, report_read)) as physical,
                    closing(AlignTrainingRows(physical, tuple(sorted(dates)), snapshot.listing_intervals)) as aligned,
                ):
                    BuildTrainingSamples(
                        TrainingSampleBuildRequest(
                            history, snapshot.training_codes, window, samples, V5_TRAINING_PROFILE, progress,
                        ),
                        aligned_rows=aligned,
                    )
                if samples.count() == 0:
                    raise ValueError("v5_quality_samples_empty")
                after = SQLiteHistoryTrainingInputSnapshot.open(history_root, sqlite_policy=sqlite_policy).snapshot
                if after != snapshot:
                    raise ValueError("v5_quality_history_changed")
                cache.seal(samples)
                progress.publish(TomorrowTrainingProgress("sample_cache_seal", "completed", 1, 1))
            rows = samples.count()
        return V5QualitySamples(
            root,
            snapshot.snapshot_hash,
            snapshot.source_identity_hash,
            snapshot.input_descriptor_hash,
            snapshot.source_cutoff.isoformat(),
            split.content_hash,
            identity,
            rows,
            snapshot.universe_count,
            settings.fit_dates,
            reused,
        )


__all__ = [
    "PrepareV5QualitySamples",
    "V5QualitySamples",
]
