"""Resolve the training cadence from the active history and bundle identities."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from trader.domain.research.h1_point_in_time import canonical_hash
from trader.domain.research.history_control import (
    HistoryActiveSnapshot,
    HistoryTrainingDueState,
    calculate_history_training_due,
)
from trader.infra.research.history_control_repository import (
    HistoryControlError,
    SQLiteHistoryControlRepository,
)
from trader.infra.scoring.profiles.v3.bundle_store import (
    ActiveTomorrowBundle,
    inspect_active_tomorrow_bundle,
)


@dataclass(frozen=True)
class HistoryTrainingDueEvaluation:
    archive_root: Path
    active_snapshot: HistoryActiveSnapshot
    state: HistoryTrainingDueState
    bundle: ActiveTomorrowBundle | None


def evaluate_history_training_due(
    archive_root: Path,
    training_root: Path,
    observed_at: datetime,
) -> HistoryTrainingDueEvaluation | None:
    """Read and persist one immutable due observation.

    A missing active snapshot is intentionally returned as ``None``: callers
    project that condition as ``history_manifest_unavailable`` instead of
    fabricating a cadence baseline.
    """

    control = SQLiteHistoryControlRepository(archive_root / "control.sqlite3")
    try:
        state = control.load_state()
    except HistoryControlError:
        return None
    active = state.active_snapshot
    if active is None:
        return None
    calendar = next((item for item in state.calendars if item.content_hash == active.calendar_hash), None)
    if calendar is None:
        return None

    bundle, bundle_invalid = _active_bundle(training_root)
    if bundle_invalid:
        due_identity = "due-" + canonical_hash((active.content_hash, "invalid_bundle"))[:32]
        due = calculate_history_training_due(
            due_identity=due_identity,
            baseline_label_cutoff=None,
            current_label_cutoff=active.label_cutoff,
            calendar_dates=calendar.open_dates,
            input_revision=False,
            observed_at=observed_at,
            data_complete=False,
        )
        return HistoryTrainingDueEvaluation(archive_root, active, due, None)
    baseline_cutoff = bundle.label_cutoff if bundle is not None else None
    input_revision = bundle is not None and bundle.training_input_hash != active.content_hash
    due_identity = "due-" + canonical_hash(
        (
            active.content_hash,
            bundle.training_input_hash if bundle is not None else None,
            baseline_cutoff,
            active.label_cutoff,
            input_revision,
        )
    )[:32]
    due = calculate_history_training_due(
        due_identity=due_identity,
        baseline_label_cutoff=baseline_cutoff,
        current_label_cutoff=active.label_cutoff,
        calendar_dates=calendar.open_dates,
        input_revision=input_revision,
        observed_at=observed_at,
    )
    previous = next((item for item in state.due_states if item.due_identity == due.due_identity), None)
    if previous is None:
        control.save_due_state(due)
    else:
        due = previous
    return HistoryTrainingDueEvaluation(archive_root, active, due, bundle)


def _active_bundle(training_root: Path) -> tuple[ActiveTomorrowBundle | None, bool]:
    pointer = training_root / "tomorrow-v3" / "active-bundle.json"
    if not pointer.exists():
        return None, False
    try:
        return inspect_active_tomorrow_bundle(training_root / "tomorrow-v3"), False
    except (OSError, TypeError, ValueError):
        return None, True


__all__ = ["HistoryTrainingDueEvaluation", "evaluate_history_training_due"]
