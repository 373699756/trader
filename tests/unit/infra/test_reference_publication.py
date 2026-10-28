"""Only a validated reference from the same Shanghai date supplies a cached calendar."""

import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from tests.reference_fixtures import PreparedFixture
from trader.download.domain.history_reference import HistoryStEvidence
from trader.download.infra.akshare_calendar import AkshareCalendarTimeoutError
from trader.download.infra.reference_index import SQLiteReferenceIndex
from trader.infra.sqlite_connection import SqliteConnectionOptions, SqliteSession, SqliteTransaction

_NOW = datetime(2026, 10, 26, 20, tzinfo=ZoneInfo("Asia/Shanghai"))


def test_same_day_calendar_reuse_preserves_reference_and_avoids_supplier(tmp_path):
    publication = SQLiteReferenceIndex(tmp_path / "company_st.sqlite3")
    reference = PreparedFixture(_NOW)
    publication.publish(reference, reference.st_evidence)
    before = (tmp_path / "company_st.sqlite3").read_bytes()
    events = []

    def fetch():
        pytest.fail("same-day calendar must not depend on another supplier request")

    assert publication.calendar(_NOW.date(), fetch=fetch, progress=events.append) == reference.open_dates
    assert [(event.component, event.status) for event in events] == [("calendar", "reused")]
    assert (tmp_path / "company_st.sqlite3").read_bytes() == before


@pytest.mark.parametrize("cache", ("missing", "yesterday", "future", "corrupt", "hash_conflict", "empty_calendar"))
def test_missing_stale_or_invalid_calendar_requires_supplier(tmp_path, cache):
    publication = SQLiteReferenceIndex(tmp_path / "company_st.sqlite3")
    reference = PreparedFixture(_NOW)
    if cache != "missing":
        offset = -1 if cache == "yesterday" else 1 if cache == "future" else 0
        seed = PreparedFixture(_NOW + timedelta(days=offset))
        publication.publish(seed, seed.st_evidence)
    path = tmp_path / "company_st.sqlite3"
    if cache == "corrupt":
        path.write_text("{", encoding="utf-8")
    elif cache in {"hash_conflict", "empty_calendar"}:
        with SqliteSession(path) as connection, SqliteTransaction(connection):
            connection.run(
                "UPDATE reference_publication SET content_hash=zeroblob(32)"
                if cache == "hash_conflict"
                else "DELETE FROM trading_calendar"
            )
    events, calls = [], []

    def fetch():
        calls.append(True)
        return reference.open_dates

    assert publication.calendar(_NOW.date(), fetch=fetch, progress=events.append) == reference.open_dates
    assert calls == [True]
    assert [event.status for event in events] == ["running", "complete"]
    assert bool(events[0].failure) == (cache in {"corrupt", "hash_conflict", "empty_calendar"})


def test_expired_calendar_does_not_hide_timeout_or_replace_previous_publication(tmp_path):
    publication = SQLiteReferenceIndex(tmp_path / "company_st.sqlite3")
    seed = PreparedFixture(_NOW - timedelta(days=1))
    publication.publish(seed, seed.st_evidence)
    before = (tmp_path / "company_st.sqlite3").read_bytes()

    def fetch():
        raise AkshareCalendarTimeoutError(30)

    with pytest.raises(AkshareCalendarTimeoutError):
        publication.calendar(_NOW.date(), fetch=fetch, progress=None)
    assert (tmp_path / "company_st.sqlite3").read_bytes() == before


def test_publications_pin_versions_and_preserve_supplier_check_date(tmp_path):
    index = SQLiteReferenceIndex(tmp_path / "company_st.sqlite3")
    source = HistoryStEvidence("600001", (_NOW - timedelta(days=30)).date(), "ever_st")
    index.merge_st((source,))
    first = replace(PreparedFixture(_NOW), st_evidence=(replace(source, checked_on=_NOW.date()),))
    index.publish(first, (source,))
    next_day = _NOW + timedelta(days=1)
    second = replace(
        PreparedFixture(next_day),
        official_universe=(replace(first.official_universe[0], name="新名称"),),
        st_evidence=(replace(source, checked_on=next_day.date()),),
    )
    index.publish(second, (source,))
    index.merge_st((HistoryStEvidence(source.code, next_day.date(), "clear"),), (source,))
    assert index.read_st() == (source,)
    assert index.read(first.identity) == first
    assert index.read() == second
    assert not (tmp_path / "inputs.json").exists()


def test_same_day_republish_keeps_first_publication_time(tmp_path):
    index = SQLiteReferenceIndex(tmp_path / "company_st.sqlite3")
    first = PreparedFixture(_NOW)
    index.publish(first, first.st_evidence)
    later = replace(first, checked_at=_NOW + timedelta(minutes=5))
    index.publish(later, later.st_evidence)
    assert index.read() == first
    assert index.identity() == first.identity


def test_failed_member_insert_rolls_back_publication_and_current_pointer(tmp_path):
    index = SQLiteReferenceIndex(tmp_path / "company_st.sqlite3")
    first = PreparedFixture(_NOW)
    index.publish(first, first.st_evidence)
    second = PreparedFixture(_NOW + timedelta(days=1))
    with SqliteSession(index.path) as connection:
        connection.run(
            "CREATE TRIGGER reject_member BEFORE INSERT ON reference_member BEGIN SELECT RAISE(ABORT,'fixture'); END"
        )
    with pytest.raises(ValueError, match="unavailable"):
        index.publish(second, second.st_evidence)
    assert index.read() == first
    with pytest.raises(ValueError, match="missing"):
        index.read(second.identity)


def test_cross_security_member_is_rejected_by_foreign_key(tmp_path):
    index = SQLiteReferenceIndex(tmp_path / "company_st.sqlite3")
    reference = PreparedFixture(_NOW)
    index.publish(reference, reference.st_evidence)
    with SqliteSession(index.path, SqliteConnectionOptions(foreign_keys=True)) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.run("UPDATE reference_member SET code='600002'")


def test_old_st_schema_is_not_accepted_as_reference_database(tmp_path):
    path = tmp_path / "company_st.sqlite3"
    with SqliteSession(path) as connection:
        connection.run("CREATE TABLE st_evidence(code TEXT,checked_on TEXT,status TEXT)")
        connection.run("PRAGMA user_version=1")
    with pytest.raises(ValueError, match="unavailable"):
        SQLiteReferenceIndex(path).read_st()
