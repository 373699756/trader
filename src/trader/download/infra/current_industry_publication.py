"""Current industry input from the validated local query database."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from trader.download.infra.baostock_industry import BaoStockIndustryClient
from trader.download.infra.industry_query_index import IndustryQuery, SQLiteIndustryQueryIndex
from trader.infra.market_data.observations import SourceObservation
from trader.recommendation.domain.market.models import ModelIndustryReference

_CONTRACT = "baostock_dated_industry_queries"
_CURRENT_TABLE = "current_industry_query"
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_REFRESH_DAYS = 7


@dataclass(frozen=True)
class CurrentIndustryPublication:
    path: Path
    client: BaoStockIndustryClient
    minimum_rows: int = 4_000

    def fetch(self, observed_at: datetime) -> tuple[SourceObservation, ...]:
        cache = CurrentIndustryCache(self.path, self.minimum_rows)
        query = cache.latest(observed_at)
        if query is not None and (observed_at.astimezone(_SHANGHAI).date() - query.day).days < _REFRESH_DAYS:
            return _query_observations(query)
        observations = self.client.fetch(observed_at)
        rows = [
            [
                item.subject_key,
                str(item.fields["model_industry_effective_date"]),
                str(item.fields["model_industry"]),
                str(item.fields["model_industry_classification"]),
            ]
            for item in observations
        ]
        if len(rows) < self.minimum_rows:
            raise ValueError("industry_reference_snapshot_incomplete")
        day = observed_at.astimezone(_SHANGHAI).date()
        SQLiteIndustryQueryIndex(self.path, _CONTRACT, _CURRENT_TABLE).save(
            "baostock_current_industry",
            day,
            day,
            json.dumps(rows, ensure_ascii=False, separators=(",", ":")),
        )
        return cache.load(observed_at)


@dataclass(frozen=True)
class CurrentIndustryCache:
    path: Path
    minimum_rows: int = 4_000

    def load(self, observed_at: datetime) -> tuple[SourceObservation, ...]:
        query = self.latest(observed_at)
        return _query_observations(query) if query is not None else ()

    def latest(self, observed_at: datetime) -> IndustryQuery | None:
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise ValueError("industry query time must be timezone-aware")
        return self._latest_valid_query(SQLiteIndustryQueryIndex(self.path, _CONTRACT, _CURRENT_TABLE), observed_at)

    def _latest_valid_query(self, cache: SQLiteIndustryQueryIndex, observed_at: datetime) -> IndustryQuery | None:
        cursor = observed_at.astimezone(_SHANGHAI).date()
        for _ in range(64):
            query = cache.latest(cursor)
            if query is None:
                return None
            try:
                observations = _query_observations(query)
            except (TypeError, ValueError):
                observations = ()
            if len(observations) >= self.minimum_rows:
                return query
            if query.day == date.min:
                return None
            cursor = query.day - timedelta(days=1)
        return None


def _query_observations(query: IndustryQuery) -> tuple[SourceObservation, ...]:
    values = json.loads(query.payload)
    if not isinstance(values, list) or len(values) > 20_000:
        raise ValueError("industry query payload is invalid")
    source_time = datetime.combine(query.observed_on, time.min, _SHANGHAI)
    version = "industry-query:" + query.digest
    result: list[SourceObservation] = []
    codes: set[str] = set()
    for row in values:
        if not isinstance(row, list) or len(row) != 4 or not all(isinstance(item, str) for item in row):
            raise ValueError("industry query row is invalid")
        code, effective_raw, industry, classification = row
        effective = date.fromisoformat(effective_raw)
        if len(code) != 6 or not code.isdigit() or code in codes or effective > query.day:
            raise ValueError("industry query identity is invalid")
        ModelIndustryReference(industry, classification, effective, "baostock", version)
        codes.add(code)
        result.append(
            SourceObservation(
                source="baostock_industry",
                subject_key=code,
                observed_at=source_time,
                source_time=source_time,
                received_at=source_time,
                effective_at=datetime.combine(effective, time.min, _SHANGHAI),
                data_version=version,
                fields={
                    "model_industry": industry,
                    "model_industry_classification": classification,
                    "model_industry_effective_date": effective_raw,
                    "model_industry_source": "baostock",
                    "model_industry_data_version": version,
                },
                missing_reasons={},
                payload_hash="",
                status="success",
                error_code=None,
            )
        )
    return tuple(result)


__all__ = ["CurrentIndustryCache", "CurrentIndustryPublication"]
