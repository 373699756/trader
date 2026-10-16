"""Current industry input from the validated local query database."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from trader.infra.dated_query_cache import DatedQuery, SQLiteDatedQueryCache
from trader.infra.market_data.observations import SourceObservation
from trader.infra.market_data.providers.baostock_industry import BaoStockIndustryClient
from trader.infra.market_data.source_health import ModelIndustrySourceHealth
from trader.recommendation.domain.market.models import ModelIndustryReference

_CONTRACT = "baostock_dated_industry_queries"
_CURRENT_TABLE = "current_industry_queries"
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_REFRESH_DAYS = 7


@dataclass(frozen=True)
class LocalIndustrySource:
    path: Path
    client: BaoStockIndustryClient
    minimum_rows: int = 4_000

    def load_cached(self, observed_at: datetime) -> tuple[SourceObservation, ...]:
        query = self._query(observed_at)
        return _query_observations(query) if query is not None else ()

    def fetch(self, observed_at: datetime) -> tuple[SourceObservation, ...]:
        query = self._query(observed_at)
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
        SQLiteDatedQueryCache(self.path, _CONTRACT, _CURRENT_TABLE).save(
            "baostock_current_industry",
            day,
            day,
            json.dumps(rows, ensure_ascii=False, separators=(",", ":")),
        )
        return self.load_cached(observed_at)

    def health(self) -> ModelIndustrySourceHealth:
        return self.client.health()

    def _query(self, observed_at: datetime) -> DatedQuery | None:
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise ValueError("industry query time must be timezone-aware")
        historical = self._latest_valid_query(SQLiteDatedQueryCache(self.path, _CONTRACT), observed_at)
        current = self._latest_valid_query(SQLiteDatedQueryCache(self.path, _CONTRACT, _CURRENT_TABLE), observed_at)
        if historical is None:
            return current
        if current is None:
            return historical
        return max((historical, current), key=lambda query: (query.day, query.observed_on, query is current))

    def _latest_valid_query(self, cache: SQLiteDatedQueryCache, observed_at: datetime) -> DatedQuery | None:
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


def _query_observations(query: DatedQuery) -> tuple[SourceObservation, ...]:
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


__all__ = ["LocalIndustrySource"]
