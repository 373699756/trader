"""Historical interval projection of the shared industry query database."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from trader.download.domain.baostock_daily import BaoStockIndustryInterval
from trader.infra.dated_query_cache import SQLiteDatedQueryCache

_LOGGER = logging.getLogger(__name__)
_CONTRACT = "baostock_dated_industry_queries"


@dataclass(frozen=True)
class HistoryIndustryQueryCache:
    """Used under the history maintenance lock; each complete date commits separately."""

    path: Path

    def prepare(self, source: str, dates: tuple[date, ...]) -> None:
        if not source or not dates or len(dates) > 64 or len(set(dates)) != len(dates):
            raise ValueError("industry_cache_dates_invalid")
        SQLiteDatedQueryCache(self.path, _CONTRACT).prepare()

    def read(self, source: str, day: date, as_of: date) -> tuple[BaoStockIndustryInterval, ...] | None:
        query = SQLiteDatedQueryCache(self.path, _CONTRACT).read(day, as_of, source=source)
        if query is None:
            return None
        try:
            return _decode(query.payload, day)
        except (TypeError, ValueError):
            _LOGGER.warning("industry_query_cache_invalid_requery day=%s", day)
            return None

    def save(self, source: str, day: date, as_of: date, facts: tuple[BaoStockIndustryInterval, ...]) -> None:
        if not facts:
            return
        payload = json.dumps(
            [[item.code, item.effective_from.isoformat(), item.industry, item.classification] for item in facts],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        _decode(payload, day)
        if as_of < day:
            raise ValueError("industry_cache_observation_invalid")
        SQLiteDatedQueryCache(self.path, _CONTRACT).save(source, day, as_of, payload)


def _decode(payload: str, day: date) -> tuple[BaoStockIndustryInterval, ...]:
    values = json.loads(payload)
    if not isinstance(values, list) or not values or len(values) > 20000:
        raise ValueError("industry_cache_rows_invalid")
    facts = []
    for row in values:
        if not isinstance(row, list) or len(row) != 4 or not all(isinstance(item, str) for item in row):
            raise ValueError("industry_cache_row_invalid")
        fact = BaoStockIndustryInterval(row[0], date.fromisoformat(row[1]), None, row[2], row[3])
        if fact.effective_from > day:
            raise ValueError("industry_cache_future_fact")
        facts.append(fact)
    if len({item.code for item in facts}) != len(facts):
        raise ValueError("industry_cache_duplicate_code")
    return tuple(facts)


__all__ = ["HistoryIndustryQueryCache"]
