from dataclasses import dataclass
from datetime import date, datetime, timedelta

from trader.application.research.h1_point_in_time import H1PointInTimeDownloadService
from trader.application.research.historical_screening import HistoricalSecurity
from trader.domain.research.h1_point_in_time import H1PointInTimeRecord, H1PointInTimeSpec
from trader.domain.research.historical_screening import HistoricalPriceBar


def _records(code: str, strategy: str) -> tuple[H1PointInTimeRecord, ...]:
    digest = "b" * 64
    rows = []
    start = date(2026, 8, 28)
    for offset in range(3):
        day = start + timedelta(days=offset)
        bar = HistoricalPriceBar(day, 10, 10.2, 10.3, 9.9, 100, 1000, 2, None, "qfq", "fixture")
        stamp = f"{day.isoformat()}T11:20:00+08:00" if strategy == "today" else f"{day.isoformat()}T14:50:00+08:00"
        rows.append(
            H1PointInTimeRecord(
                strategy, code, day, datetime.fromisoformat(stamp), bar, 10.1, 50, 500, digest, digest, digest
            )
        )
    return tuple(rows)


class _Universe:
    def fetch(self):
        return (
            HistoricalSecurity("600001", "main", "A", False, False),
            HistoricalSecurity("600002", "main", "B", False, False),
        )


class _History:
    def fetch(self, code, spec):
        return _records(code, spec.strategy)


@dataclass
class _Archive:
    complete: frozenset[str] = frozenset()

    def __post_init__(self):
        self.universe = ()
        self.saved = {}
        self.failures = {}

    def registered_universe(self, _strategy):
        return self.universe

    def register_universe(self, _spec, universe):
        self.universe = tuple(universe)

    def completed_codes(self, _strategy):
        return self.complete

    def save_records(self, _spec, code, records):
        self.saved[code] = tuple(records)

    def record_failure(self, _spec, code, error_code):
        self.failures[code] = error_code

    def audit(self, _spec):
        raise NotImplementedError


def test_h1_download_is_bounded_resumable_and_strategy_scoped():
    archive = _Archive(frozenset({"600001"}))
    result = H1PointInTimeDownloadService(_Universe(), _History(), archive, workers=2).execute(
        H1PointInTimeSpec("today")
    )
    assert result.strategy == "today"
    assert result.previously_completed == 1
    assert result.attempted == 1
    assert result.downloaded == 1
    assert not archive.failures
    assert set(archive.saved) == {"600002"}
