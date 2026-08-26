"""Pure immutable identities for preregistered score research windows."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Literal

_IDENTITY = re.compile(r"^[a-z0-9_]{1,64}$")
SCORE_RESEARCH_OBSERVATION_CUTOFF = time(14, 50)


@dataclass(frozen=True)
class ScoreResearchSpec:
    """Bind a score experiment to dates and a deterministic random namespace."""

    research_identity: str
    preregistered_on: date
    historical_dates: tuple[date, ...]
    historical_replacement_dates: tuple[date, ...]
    forward_dates: tuple[date, ...]
    bootstrap_master_seed: int
    maximum_historical_days: int = 40
    historical_window_mode: Literal["retrospective", "future"] = "future"
    content_hash: str = dataclasses.field(init=False)

    def __post_init__(self) -> None:
        _validate_spec_identity(self)
        _validate_spec_dates(self)
        payload = {
            field.name: _canonical(getattr(self, field.name)) for field in dataclasses.fields(self) if field.init
        }
        encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        object.__setattr__(self, "content_hash", hashlib.sha256(encoded.encode()).hexdigest())

    @property
    def allowed_historical_dates(self) -> frozenset[date]:
        return frozenset((*self.historical_dates, *self.historical_replacement_dates))


@dataclass(frozen=True)
class ScoreResearchWindowCoverage:
    """Typed coverage of one immutable planned-date window."""

    recorded_dates: tuple[date, ...]
    missed_dates: tuple[date, ...]
    maximum_attainable_days: int
    next_planned_date: date | None
    state: Literal["collecting", "complete", "failed"]

    @property
    def complete(self) -> bool:
        return self.state == "complete"

    @property
    def recoverable(self) -> bool:
        return self.state != "failed"


@dataclass(frozen=True)
class ScoreResearchCoverage:
    historical: ScoreResearchWindowCoverage
    forward: ScoreResearchWindowCoverage


def assess_score_research_coverage(
    spec: ScoreResearchSpec,
    recorded_dates: Iterable[date],
    *,
    as_of: datetime,
    observation_cutoff: time = SCORE_RESEARCH_OBSERVATION_CUTOFF,
) -> ScoreResearchCoverage:
    """Assess fixed dates against the timezone-aware observation cutoff."""

    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("research coverage clock must be timezone-aware")
    recorded = frozenset(recorded_dates)
    return ScoreResearchCoverage(
        historical=_assess_window(spec.historical_dates, recorded, as_of, observation_cutoff),
        forward=_assess_window(spec.forward_dates, recorded, as_of, observation_cutoff),
    )


def _assess_window(
    planned_dates: tuple[date, ...],
    recorded_dates: frozenset[date],
    as_of: datetime,
    observation_cutoff: time,
) -> ScoreResearchWindowCoverage:
    current_date = as_of.date()
    current_time = as_of.timetz().replace(tzinfo=None)

    def closed(value: date) -> bool:
        return value < current_date or (value == current_date and current_time >= observation_cutoff)

    recorded = tuple(value for value in planned_dates if value in recorded_dates)
    missed = tuple(value for value in planned_dates if closed(value) and value not in recorded_dates)
    next_planned = next(
        (value for value in planned_dates if not closed(value) and value not in recorded_dates),
        None,
    )
    state: Literal["collecting", "complete", "failed"]
    if len(recorded) == len(planned_dates):
        state = "complete"
    elif missed:
        state = "failed"
    else:
        state = "collecting"
    return ScoreResearchWindowCoverage(
        recorded_dates=recorded,
        missed_dates=missed,
        maximum_attainable_days=len(planned_dates) - len(missed),
        next_planned_date=next_planned,
        state=state,
    )


def _validate_spec_identity(spec: ScoreResearchSpec) -> None:
    if _IDENTITY.fullmatch(spec.research_identity) is None:
        raise ValueError("research identity must be a bounded lowercase identifier")
    if spec.historical_window_mode not in {"retrospective", "future"}:
        raise ValueError("research historical window mode is invalid")
    if spec.bootstrap_master_seed < 1:
        raise ValueError("research bootstrap seed must be positive")


def _validate_spec_dates(spec: ScoreResearchSpec) -> None:
    if not spec.historical_dates or not spec.forward_dates:
        raise ValueError("research spec requires historical and forward dates")
    if spec.maximum_historical_days < 1 or len(spec.historical_dates) != spec.maximum_historical_days:
        raise ValueError("research spec historical dates must match its fixed maximum")
    all_dates = (*spec.historical_dates, *spec.historical_replacement_dates, *spec.forward_dates)
    if len(set(all_dates)) != len(all_dates):
        raise ValueError("research spec dates must be unique and non-overlapping")
    if (
        tuple(sorted(spec.historical_dates)) != spec.historical_dates
        or tuple(sorted(spec.forward_dates)) != spec.forward_dates
    ):
        raise ValueError("research historical and forward dates must be strictly increasing")
    if spec.historical_replacement_dates != tuple(sorted(spec.historical_replacement_dates, reverse=True)):
        raise ValueError("historical replacements must be nearest-first")
    if spec.historical_window_mode == "future" and spec.preregistered_on >= spec.historical_dates[0]:
        raise ValueError("research spec must be registered before the first planned observation")
    if spec.historical_dates[-1] >= spec.forward_dates[0]:
        raise ValueError("research forward window must follow its historical window")


def _dates(*values: str) -> tuple[date, ...]:
    return tuple(date.fromisoformat(value) for value in values)


def _weekdays(start: date, end: date) -> tuple[date, ...]:
    return tuple(
        date.fromordinal(ordinal)
        for ordinal in range(start.toordinal(), end.toordinal() + 1)
        if date.fromordinal(ordinal).weekday() < 5
    )


def _canonical(value: object) -> object:
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, tuple):
        return [_canonical(item) for item in value]
    return value


SCORE_P0_V1_SPEC = ScoreResearchSpec(
    research_identity="score_p0_v1",
    preregistered_on=date(2026, 8, 11),
    historical_dates=_dates(
        "2026-06-15",
        "2026-06-16",
        "2026-06-17",
        "2026-06-18",
        "2026-06-22",
        "2026-06-23",
        "2026-06-24",
        "2026-06-25",
        "2026-06-26",
        "2026-06-29",
        "2026-06-30",
        "2026-07-01",
        "2026-07-02",
        "2026-07-03",
        "2026-07-06",
        "2026-07-07",
        "2026-07-08",
        "2026-07-09",
        "2026-07-10",
        "2026-07-13",
        "2026-07-14",
        "2026-07-15",
        "2026-07-16",
        "2026-07-17",
        "2026-07-20",
        "2026-07-21",
        "2026-07-22",
        "2026-07-23",
        "2026-07-24",
        "2026-07-27",
        "2026-07-28",
        "2026-07-29",
        "2026-07-30",
        "2026-07-31",
        "2026-08-03",
        "2026-08-04",
        "2026-08-05",
        "2026-08-06",
        "2026-08-07",
        "2026-08-10",
    ),
    historical_replacement_dates=_dates(
        "2026-06-12",
        "2026-06-11",
        "2026-06-10",
        "2026-06-09",
        "2026-06-08",
        "2026-06-05",
        "2026-06-04",
        "2026-06-03",
        "2026-06-02",
        "2026-06-01",
        "2026-05-29",
        "2026-05-28",
        "2026-05-27",
        "2026-05-26",
        "2026-05-25",
        "2026-05-22",
        "2026-05-21",
        "2026-05-20",
        "2026-05-19",
        "2026-05-18",
    ),
    forward_dates=_weekdays(date(2026, 11, 2), date(2026, 11, 27)),
    bootstrap_master_seed=20260811,
    historical_window_mode="retrospective",
)

SCORE_P0_V2_SPEC = ScoreResearchSpec(
    research_identity="score_p0_v2",
    preregistered_on=date(2026, 8, 20),
    historical_dates=_dates(
        "2026-08-21",
        "2026-08-24",
        "2026-08-25",
        "2026-08-26",
        "2026-08-27",
        "2026-08-28",
        "2026-08-31",
        "2026-09-01",
        "2026-09-02",
        "2026-09-03",
        "2026-09-04",
        "2026-09-07",
        "2026-09-08",
        "2026-09-09",
        "2026-09-10",
        "2026-09-11",
        "2026-09-14",
        "2026-09-15",
        "2026-09-16",
        "2026-09-17",
        "2026-09-18",
        "2026-09-21",
        "2026-09-22",
        "2026-09-23",
        "2026-09-24",
        "2026-09-28",
        "2026-09-29",
        "2026-09-30",
        "2026-10-08",
        "2026-10-09",
        "2026-10-12",
        "2026-10-13",
        "2026-10-14",
        "2026-10-15",
        "2026-10-16",
        "2026-10-19",
        "2026-10-20",
        "2026-10-21",
        "2026-10-22",
        "2026-10-23",
    ),
    historical_replacement_dates=(),
    forward_dates=_dates(
        "2026-10-26",
        "2026-10-27",
        "2026-10-28",
        "2026-10-29",
        "2026-10-30",
        "2026-11-02",
        "2026-11-03",
        "2026-11-04",
        "2026-11-05",
        "2026-11-06",
        "2026-11-09",
        "2026-11-10",
        "2026-11-11",
        "2026-11-12",
        "2026-11-13",
        "2026-11-16",
        "2026-11-17",
        "2026-11-18",
        "2026-11-19",
        "2026-11-20",
    ),
    bootstrap_master_seed=20260820,
)

ACTIVE_SCORE_RESEARCH_SPEC = SCORE_P0_V2_SPEC
_SPEC_BY_IDENTITY = {
    SCORE_P0_V1_SPEC.research_identity: SCORE_P0_V1_SPEC,
    SCORE_P0_V2_SPEC.research_identity: SCORE_P0_V2_SPEC,
}


def get_score_research_spec(research_identity: str) -> ScoreResearchSpec:
    try:
        return _SPEC_BY_IDENTITY[research_identity]
    except KeyError as exc:
        raise ValueError("unknown score research identity") from exc


__all__ = [
    "ACTIVE_SCORE_RESEARCH_SPEC",
    "SCORE_P0_V1_SPEC",
    "SCORE_P0_V2_SPEC",
    "SCORE_RESEARCH_OBSERVATION_CUTOFF",
    "ScoreResearchCoverage",
    "ScoreResearchSpec",
    "ScoreResearchWindowCoverage",
    "assess_score_research_coverage",
    "get_score_research_spec",
]
