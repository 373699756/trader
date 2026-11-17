"""Read-only point-in-time selection over committed recommendation observations."""

from collections.abc import Callable
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from trader.recommendation.application.pipeline.freeze_publish.freeze_events import FreezeObservation, HistoricalFreezeObservation
from trader.recommendation.domain.publication.models import Strategy
from trader.training.application.research_audit import CommittedResearchAudit, HasCompleteResearchEvidence, HistoricalResearchAudit

_SHANGHAI = ZoneInfo("Asia/Shanghai")


class ResearchTraceIndex:
    def __init__(self, read_day: Callable[[date], tuple[FreezeObservation | HistoricalFreezeObservation, ...]]) -> None:
        self._read_day = read_day

    def latest_point_in_time_observation(
        self,
        trade_date: date,
        strategy: Strategy,
        *,
        cutoff: time = time(hour=15, minute=0),
    ) -> FreezeObservation | HistoricalFreezeObservation | None:
        """Return the latest complete local input that existed by the research cutoff."""

        if cutoff.tzinfo is not None:
            raise ValueError("research cutoff must be a Shanghai wall-clock time")
        cutoff_at = datetime.combine(trade_date, cutoff, tzinfo=_SHANGHAI)
        eligible: list[tuple[datetime, FreezeObservation | HistoricalFreezeObservation]] = []
        for observation in self._read_day(trade_date):
            audit = observation.research_audit
            if (
                observation.event.strategy is not strategy
                or observation.event.observed_at.astimezone(_SHANGHAI) > cutoff_at
                or not isinstance(audit, (CommittedResearchAudit, HistoricalResearchAudit))
                or not HasCompleteResearchEvidence(audit)
                or audit.shadow_mode != "control_copy"
                or audit.freeze_version != observation.event.freeze_version
                or audit.freeze_hash != observation.event.freeze_hash
                or audit.deepseek_request_delta != 0
                or audit.input_observed_at is None
                or audit.input_observed_at.tzinfo is None
                or audit.input_observed_at.astimezone(_SHANGHAI) > cutoff_at
                or not audit.point_in_time_population
                or not audit.point_in_time_population_hash
            ):
                continue
            if (
                observation.event.observed_at.astimezone(_SHANGHAI).date() != trade_date
                or audit.input_observed_at.astimezone(_SHANGHAI).date() != trade_date
            ):
                continue
            eligible.append((audit.input_observed_at, observation))
        if not eligible:
            return None
        return max(
            eligible,
            key=lambda item: (
                item[0],
                item[1].event.observed_at,
                item[1].event.freeze_version,
            ),
        )[1]


__all__ = ["ResearchTraceIndex"]
