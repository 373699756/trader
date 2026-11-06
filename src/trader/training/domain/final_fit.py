"""Latest mature training dates for an explicitly unvalidated final model refit."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from trader.infra.serialization.canonical_codec import CanonicalDigest
from trader.training.domain.training_dates import MAX_TRAINING_SESSIONS

FINAL_FIT_CONTRACT = "latest_mature_labels_final_refit"


@dataclass(frozen=True)
class FinalFitDatePlan:
    source_cutoff: date
    maturity_sessions: int
    fit_dates: tuple[date, ...]
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if (
            type(self.source_cutoff) is not date
            or type(self.maturity_sessions) is not int
            or self.maturity_sessions not in {1, 5}
            or type(self.fit_dates) is not tuple
            or not 1 <= len(self.fit_dates) <= MAX_TRAINING_SESSIONS
            or any(type(day) is not date for day in self.fit_dates)
            or self.fit_dates != tuple(sorted(set(self.fit_dates)))
            or self.fit_dates[-1] >= self.source_cutoff
            or (self.source_cutoff - self.fit_dates[-1]).days < self.maturity_sessions
        ):
            raise ValueError("final_fit_date_plan_invalid")
        object.__setattr__(self, "content_hash", CanonicalDigest((
            FINAL_FIT_CONTRACT, self.source_cutoff, self.maturity_sessions, self.fit_dates,
        )))


def BuildFinalFitDatePlan(
    calendar: tuple[date, ...],
    *,
    source_cutoff: date,
    fit_sessions: int,
    maturity_sessions: int,
) -> FinalFitDatePlan:
    """Select exactly N final signals whose full head horizon is observed at cutoff."""
    if (
        type(calendar) is not tuple
        or not calendar
        or len(calendar) > MAX_TRAINING_SESSIONS
        or any(type(day) is not date for day in calendar)
        or calendar != tuple(sorted(set(calendar)))
        or type(source_cutoff) is not date
        or source_cutoff not in calendar
        or type(fit_sessions) is not int
        or fit_sessions < 1
        or type(maturity_sessions) is not int
        or maturity_sessions not in {1, 5}
    ):
        raise ValueError("final_fit_calendar_or_settings_invalid")
    end = calendar.index(source_cutoff) + 1 - maturity_sessions
    start = end - fit_sessions
    if start < 0:
        raise ValueError("final_fit_calendar_insufficient")
    return FinalFitDatePlan(source_cutoff, maturity_sessions, calendar[start:end])


__all__ = [
    "FINAL_FIT_CONTRACT",
    "FinalFitDatePlan",
    "BuildFinalFitDatePlan",
]
