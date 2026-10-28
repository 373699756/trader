"""Read-only point-in-time selection over committed recommendation observations."""

from collections.abc import Callable
from datetime import date, datetime, time
from typing import cast
from zoneinfo import ZoneInfo

from trader.recommendation.application.pipeline.freeze_publish.freeze_events import FreezeObservation
from trader.recommendation.domain.publication.models import Strategy
from trader.training.application.research_audit import RESEARCH_AUDIT_SCHEMA_VERSION, CommittedResearchAudit

_SHANGHAI = ZoneInfo("Asia/Shanghai")


class ResearchTraceIndex:
    def __init__(self, read_day: Callable[[date], tuple[FreezeObservation, ...]]) -> None:
        self._read_day = read_day

    def latest_point_in_time_observation(
        self,
        trade_date: date,
        strategy: Strategy,
        *,
        cutoff: time = time(hour=15, minute=0),
    ) -> FreezeObservation | None:
        """Return the latest complete local input that existed by the research cutoff."""

        if cutoff.tzinfo is not None:
            raise ValueError("research cutoff must be a Shanghai wall-clock time")
        cutoff_at = datetime.combine(trade_date, cutoff, tzinfo=_SHANGHAI)
        eligible: list[FreezeObservation] = []
        for observation in self._read_day(trade_date):
            audit = observation.research_audit
            if (
                observation.event.strategy is not strategy
                or observation.event.observed_at.astimezone(_SHANGHAI) > cutoff_at
                or audit is None
                or audit.schema_version != RESEARCH_AUDIT_SCHEMA_VERSION
                or audit.shadow_mode != "control_copy"
                or audit.input_observed_at is None
                or audit.input_observed_at.astimezone(_SHANGHAI) > cutoff_at
                or not audit.point_in_time_population
            ):
                continue
            if (
                observation.event.observed_at.astimezone(_SHANGHAI).date() != trade_date
                or audit.input_observed_at.astimezone(_SHANGHAI).date() != trade_date
            ):
                continue
            eligible.append(observation)
        if not eligible:
            return None
        return max(
            eligible,
            key=lambda item: (
                cast(CommittedResearchAudit, item.research_audit).input_observed_at,
                item.event.observed_at,
                item.event.freeze_version,
            ),
        )


__all__ = ["ResearchTraceIndex"]
