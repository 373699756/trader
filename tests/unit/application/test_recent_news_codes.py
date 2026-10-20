"""Only valid, recent formal lists authorize bounded company news collection."""

from dataclasses import replace
from datetime import timedelta

from tests.unit.domain.test_decision_identity import NOW, decision
from trader.recommendation.application.pipeline.freeze_publish.recent_news_codes import RecentPublishedNewsCodes
from trader.recommendation.domain.publication.decision_identity import CommittedDecisionRecord
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.infra.persistence.decision_records import SQLiteDecisionRecords


def _record(day_offset, code):
    at = NOW + timedelta(days=day_offset)
    current = decision()
    item = current.items[0]
    return CommittedDecisionRecord(
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
    sources = tuple(SQLiteDecisionRecords(tmp_path / profile / "decisions.sqlite3") for profile in ("v2", "v3"))
    for source in sources:
        source.initialize()
        for offset, code in ((-7, "600007"), (-6, "600006"), (0, "600001"), (1, "600002")):
            source.commit(_record(offset, code))
    monkeypatch.setattr(
        SQLiteDecisionRecords, "initialize", lambda *args: (_ for _ in ()).throw(AssertionError("read only"))
    )
    assert RecentPublishedNewsCodes(sources, NOW) == ("600001", "600006")
    assert all(source.list_dates(Strategy.D25) == () for source in sources)


def test_absent_published_lists_remain_empty_without_creating_database(tmp_path):
    path = tmp_path / "missing/decisions.sqlite3"
    assert RecentPublishedNewsCodes((SQLiteDecisionRecords(path),), NOW) == ()
    assert not path.parent.exists()
