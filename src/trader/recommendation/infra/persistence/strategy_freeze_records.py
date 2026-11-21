"""Route formal Tomorrow records to stable storage across profile selections."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from trader.recommendation.application.ports.freeze_records import (
    FreezeRecordPort,
    FreezeRecordRecoverySummary,
    FreezeRecordUnavailableError,
)
from trader.recommendation.domain.outcomes.tomorrow_target import TOMORROW_CLOSE_GAIN_TARGET
from trader.recommendation.domain.publication.freeze_identity import CommittedFreezeRecord
from trader.recommendation.domain.publication.historical_freeze import HistoricalFreezeRecord
from trader.recommendation.domain.publication.models import Strategy


@dataclass(frozen=True, slots=True)
class StrategyFreezeRecords:
    profile_records: FreezeRecordPort
    tomorrow_records: FreezeRecordPort
    owns_tomorrow: bool

    def initialize(self) -> None:
        self.profile_records.initialize()
        if self.owns_tomorrow:
            self.tomorrow_records.initialize()

    def commit(self, record: CommittedFreezeRecord) -> None:
        if record.strategy is Strategy.TOMORROW:
            if (
                not self.owns_tomorrow
                or dict(record.freeze.input_versions).get("prediction_target") != TOMORROW_CLOSE_GAIN_TARGET
            ):
                raise FreezeRecordUnavailableError("only the shared Tomorrow owner may commit a new Tomorrow record")
            self.tomorrow_records.commit(record)
        else:
            self.profile_records.commit(record)

    def load(self, strategy: Strategy, trade_date: date) -> CommittedFreezeRecord | None:
        if strategy is Strategy.TOMORROW:
            shared = self.tomorrow_records.load(strategy, trade_date)
            if shared is not None:
                return shared
        return self.profile_records.load(strategy, trade_date)

    def load_history(self, strategy: Strategy, trade_date: date) -> HistoricalFreezeRecord | None:
        if strategy is Strategy.TOMORROW:
            shared = self.tomorrow_records.load_history(strategy, trade_date)
            if shared is not None:
                return shared
        return self.profile_records.load_history(strategy, trade_date)

    def list_dates(
        self, strategy: Strategy, *, limit: int = 31, year: int | None = None, month: int | None = None
    ) -> tuple[date, ...]:
        dates = self.profile_records.list_dates(strategy, limit=limit, year=year, month=month)
        if strategy is not Strategy.TOMORROW:
            return dates
        shared = self.tomorrow_records.list_dates(strategy, limit=limit, year=year, month=month)
        combined = tuple(sorted({*dates, *shared}, reverse=True))
        return combined if year is not None else combined[:limit]

    def list_settlement_dates(
        self, strategy: Strategy, *, after: date | None, limit: int
    ) -> tuple[date, ...]:
        source = self.tomorrow_records if strategy is Strategy.TOMORROW else self.profile_records
        return source.list_settlement_dates(strategy, after=after, limit=limit)

    def recover(self) -> FreezeRecordRecoverySummary:
        profile = self.profile_records.recover()
        if not self.owns_tomorrow:
            return profile
        shared = self.tomorrow_records.recover()
        return FreezeRecordRecoverySummary(
            profile.recovered + shared.recovered,
            profile.quarantined + shared.quarantined,
            profile.orphaned + shared.orphaned,
        )
