"""Route formal Tomorrow records to stable storage across profile selections."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from trader.recommendation.application.ports.freeze_records import (
    FreezeRecordConflictError,
    FreezeRecordPort,
    FreezeRecordRecoverySummary,
    FreezeRecordUnavailableError,
)
from trader.recommendation.domain.outcomes.prediction_target import PredictionTarget
from trader.recommendation.domain.publication.freeze_identity import CommittedFreezeRecord
from trader.recommendation.domain.publication.historical_freeze import HistoricalFreezeRecord
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.domain.scoring.profile_identity import ScoringProfileId
from trader.recommendation.infra.persistence.freeze_records import SQLiteFreezeRecords


@dataclass(frozen=True, slots=True)
class StrategyFreezeRecords:
    profile_records: FreezeRecordPort
    shared_history: FreezeRecordPort
    owns_tomorrow: bool
    grouped_tomorrow: SQLiteFreezeRecords
    profile: ScoringProfileId
    target: PredictionTarget
    group_profiles: tuple[ScoringProfileId, ...]
    owns_grouped_database: bool = False

    def __post_init__(self) -> None:
        if self.target.strategy is not Strategy.TOMORROW or self.profile not in self.group_profiles:
            raise ValueError("Tomorrow profile group is invalid")

    def initialize(self) -> None:
        self.profile_records.initialize()
        if self.owns_grouped_database:
            self.grouped_tomorrow.initialize()

    def commit(self, record: CommittedFreezeRecord) -> None:
        if record.strategy is Strategy.TOMORROW:
            if (
                not self.owns_tomorrow
                or dict(record.freeze.input_versions).get("prediction_target") != self.target.identity
            ):
                raise FreezeRecordUnavailableError("only the shared Tomorrow owner may commit a new Tomorrow record")
            if (
                self.shared_history.load_history(Strategy.TOMORROW, record.trade_date) is not None
                or self.profile_records.load_history(Strategy.TOMORROW, record.trade_date) is not None
            ):
                raise FreezeRecordConflictError("a historical Tomorrow result is already frozen for that day")
            self.grouped_tomorrow.commit_tomorrow(record, self.target.identity, self.group_profiles, owner=self.profile)
        else:
            self.profile_records.commit(record)

    def load(self, strategy: Strategy, trade_date: date) -> CommittedFreezeRecord | None:
        if strategy is Strategy.TOMORROW:
            shared = self.shared_history.load(strategy, trade_date)
            if shared is not None:
                return shared
            profile = self.profile_records.load(strategy, trade_date)
            if profile is not None:
                return profile
            return self.grouped_tomorrow.load_tomorrow_profile(self.profile, trade_date)
        return self.profile_records.load(strategy, trade_date)

    def load_history(self, strategy: Strategy, trade_date: date) -> HistoricalFreezeRecord | None:
        if strategy is Strategy.TOMORROW:
            shared = self.shared_history.load_history(strategy, trade_date)
            if shared is not None:
                return shared
            profile = self.profile_records.load_history(strategy, trade_date)
            if profile is not None:
                return profile
            return self.grouped_tomorrow.load_tomorrow_history(self.profile, trade_date)
        return self.profile_records.load_history(strategy, trade_date)

    def list_dates(
        self, strategy: Strategy, *, limit: int = 31, year: int | None = None, month: int | None = None
    ) -> tuple[date, ...]:
        dates = self.profile_records.list_dates(strategy, limit=limit, year=year, month=month)
        if strategy is not Strategy.TOMORROW:
            return dates
        shared = self.shared_history.list_dates(strategy, limit=limit, year=year, month=month)
        grouped = self.grouped_tomorrow.list_tomorrow_profile_dates(self.profile, limit=limit, year=year, month=month)
        combined = tuple(sorted({*dates, *shared, *grouped}, reverse=True))
        return combined if year is not None else combined[:limit]

    def list_settlement_dates(
        self, strategy: Strategy, *, after: date | None, limit: int
    ) -> tuple[date, ...]:
        if strategy is not Strategy.TOMORROW:
            return self.profile_records.list_settlement_dates(strategy, after=after, limit=limit)
        grouped = self.grouped_tomorrow.list_settlement_dates(strategy, after=after, limit=limit)
        shared = self.shared_history.list_settlement_dates(strategy, after=after, limit=limit)
        return tuple(sorted({*grouped, *shared}))[:limit]

    def tomorrow_settlement_records(self, trade_date: date) -> tuple[CommittedFreezeRecord, ...]:
        grouped = self.grouped_tomorrow.tomorrow_records(trade_date)
        if grouped:
            return grouped
        shared = self.shared_history.load(Strategy.TOMORROW, trade_date)
        return () if shared is None else (shared,)

    def recover(self) -> FreezeRecordRecoverySummary:
        profile = self.profile_records.recover()
        if not self.owns_grouped_database:
            return profile
        shared = self.grouped_tomorrow.recover()
        return FreezeRecordRecoverySummary(
            profile.recovered + shared.recovered,
            profile.quarantined + shared.quarantined,
            profile.orphaned + shared.orphaned,
        )
