"""Pure training cadence and sample invalidation rules."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Literal
from zoneinfo import ZoneInfo

from trader.infra.serialization.canonical_codec import CanonicalDigest

_IDENTITY = re.compile(r"^[a-z0-9_.:-]{1,128}$")
_SHANGHAI = ZoneInfo("Asia/Shanghai")


TrainingDueReason = Literal[
    "not_due",
    "initial_training_required",
    "cadence_due",
    "input_revision_due",
    "training_contract_due",
    "data_incomplete",
]


def _require_identity(value: str, label: str) -> None:
    if _IDENTITY.fullmatch(value) is None:
        raise ValueError(f"history {label} is invalid")


def _require_shanghai(value: datetime, label: str) -> None:
    if value.tzinfo != _SHANGHAI:
        raise ValueError(f"history {label} must use Asia/Shanghai")


@dataclass(frozen=True)
class TrainingDueState:
    due_identity: str
    reason: TrainingDueReason
    baseline_label_cutoff: date | None
    current_label_cutoff: date | None
    matured_label_days_since_training: int
    input_revision: bool
    observed_at: datetime
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _require_identity(self.due_identity, "training due identity")
        _require_shanghai(self.observed_at, "training due observation")
        valid = (
            self.reason == "initial_training_required"
            and self.baseline_label_cutoff is None
            and self.current_label_cutoff is not None
            or self.reason == "cadence_due"
            and self.baseline_label_cutoff is not None
            and self.current_label_cutoff is not None
            and self.matured_label_days_since_training >= 20
            or self.reason == "input_revision_due"
            and self.input_revision
            or self.reason == "training_contract_due"
            and self.baseline_label_cutoff is not None
            and self.current_label_cutoff is not None
            or self.reason == "not_due"
            and not self.input_revision
            and self.matured_label_days_since_training < 20
            or self.reason == "data_incomplete"
        )
        if self.matured_label_days_since_training < 0 or not valid:
            raise ValueError("history training due state is invalid")
        object.__setattr__(self, "content_hash", CanonicalDigest(self))

    @property
    def training_due(self) -> bool:
        return self.reason in {
            "initial_training_required",
            "cadence_due",
            "input_revision_due",
            "training_contract_due",
        }


@dataclass(frozen=True)
class TrainingDueRequest:
    due_identity: str
    baseline_label_cutoff: date | None
    current_label_cutoff: date | None
    calendar_dates: tuple[date, ...]
    input_revision: bool
    observed_at: datetime
    data_complete: bool = True
    training_contract_changed: bool = False


def CalculateTrainingDue(request: TrainingDueRequest) -> TrainingDueState:
    """Derive the immutable training cadence from mature trading sessions.

    The count is deliberately based on the exchange calendar rather than elapsed
    wall-clock time.  A missing input is represented as ``data_incomplete`` and
    never advances the training baseline.
    """

    dates = tuple(request.calendar_dates)
    if not dates or dates != tuple(sorted(set(dates))):
        raise ValueError("history training calendar is invalid")
    current = request.current_label_cutoff
    if current is not None and current not in dates:
        raise ValueError("history training current label cutoff is outside the calendar")
    data_complete = request.data_complete
    if request.baseline_label_cutoff is not None and current is not None and request.baseline_label_cutoff > current:
        data_complete = False
    if not data_complete or current is None:
        reason: TrainingDueReason = "data_incomplete"
        matured = 0
    else:
        matured = (
            sum(day > request.baseline_label_cutoff and day <= current for day in dates)
            if request.baseline_label_cutoff is not None
            else 0
        )
        if request.baseline_label_cutoff is None:
            reason = "initial_training_required"
        elif request.input_revision:
            reason = "input_revision_due"
        elif request.training_contract_changed:
            reason = "training_contract_due"
        elif matured >= 20:
            reason = "cadence_due"
        else:
            reason = "not_due"
    return TrainingDueState(
        request.due_identity,
        reason,
        request.baseline_label_cutoff,
        current,
        matured,
        request.input_revision,
        request.observed_at,
    )


def CalculateTrainingCacheInvalidationDates(
    calendar_dates: tuple[date, ...],
    revised_dates: tuple[date, ...],
    *,
    dependency_sessions: int = 60,
) -> tuple[date, ...]:
    """Expand revised inputs to their T+1 label and bounded feature dependants."""

    calendar = tuple(calendar_dates)
    revisions = tuple(revised_dates)
    if (
        dependency_sessions < 0
        or not calendar
        or calendar != tuple(sorted(set(calendar)))
        or revisions != tuple(sorted(set(revisions)))
        or any(day not in calendar for day in revisions)
    ):
        raise ValueError("history training cache invalidation input is invalid")
    positions = {day: position for position, day in enumerate(calendar)}
    invalidated: set[date] = set()
    for day in revisions:
        position = positions[day]
        invalidated.update(calendar[max(0, position - 1) : position + dependency_sessions + 1])
    return tuple(sorted(invalidated))


__all__ = [
    "TrainingDueReason",
    "TrainingDueState",
    "TrainingDueRequest",
    "CalculateTrainingDue",
    "CalculateTrainingCacheInvalidationDates",
]
