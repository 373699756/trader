"""Only valid, recent formal lists authorize bounded company news collection."""

from dataclasses import replace
from datetime import timedelta

from tests.unit.domain.test_freeze_identity import NOW, freeze
from trader.recommendation.application.pipeline.freeze_publish.recent_news_codes import RecentPublishedNewsCodes
from trader.recommendation.domain.publication.freeze_identity import CommittedFreezeRecord
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.infra.persistence.freeze_records import SQLiteFreezeRecords


def _record(day_offset, code):
    at = NOW + timedelta(days=day_offset)
    current = freeze()
    item = current.items[0]
    return CommittedFreezeRecord(
        replace(
            current,
            trade_date=at.date(),
            observed_at=at,
            items=(replace(item, code=code, quote=replace(item.quote, code=code, source_time=at)),),
        ),
        at,
        "scheduled",
    )


def test_news_population_uses_seven_calendar_days_and_deduplicates_profiles(tmp_path, monkeypatch):
    sources = tuple(SQLiteFreezeRecords(tmp_path / profile / "freeze.sqlite3") for profile in ("v2", "v3"))
    for source in sources:
        source.initialize()
        for offset, code in ((-7, "600007"), (-6, "600006"), (0, "600001"), (1, "600002")):
            source.commit(_record(offset, code))
    monkeypatch.setattr(
        SQLiteFreezeRecords, "initialize", lambda *args: (_ for _ in ()).throw(AssertionError("read only"))
    )
    assert RecentPublishedNewsCodes(sources, NOW) == ("600001", "600006")
    assert all(source.list_dates(Strategy.D25) == () for source in sources)


def test_absent_published_lists_remain_empty_without_creating_database(tmp_path):
    path = tmp_path / "missing/freeze.sqlite3"
    assert RecentPublishedNewsCodes((SQLiteFreezeRecords(path),), NOW) == ()
    assert not path.parent.exists()
