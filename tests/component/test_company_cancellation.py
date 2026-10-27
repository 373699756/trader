"""Cancellation interrupts company I/O without discarding durable pages or publication."""

from threading import Event

import pytest

from tests.component import test_company_evidence as fixtures
from tests.unit.infra.test_company_sources import Session
from trader.download.application.company_evidence import CompanyEvidenceDependencies
from trader.download.domain.company_evidence import DisclosurePage, DisclosurePartition
from trader.download.infra.company_evidence_index import SQLiteCompanyEvidenceIndex
from trader.download.infra.company_structured_source import EastmoneyCompanySource
from trader.infra.http_contracts import HttpCancelled
from trader.infra.sqlite_connection import SqliteSession, SqliteTransaction


def test_cancellation_before_initialization_does_not_create_database(tmp_path):
    index = SQLiteCompanyEvidenceIndex(tmp_path / "evidence.sqlite3", cancel_requested=lambda: True)
    with pytest.raises(InterruptedError, match="company_sync_cancelled"):
        index.initialize()
    assert not index.path.exists()


def test_long_query_cancellation_rolls_back_only_uncommitted_changes(tmp_path):
    cancelled = Event()
    index = SQLiteCompanyEvidenceIndex(tmp_path / "evidence.sqlite3", cancel_requested=cancelled.is_set)
    index.initialize()
    day = fixtures.NOW.date()
    partition = DisclosurePartition("sse", day, day)
    with index._connect() as connection:
        connection.run("INSERT INTO partitions(market,start,end) VALUES('sse',?,?)", (str(day), str(day)))
    index.commit_page(partition, DisclosurePage((fixtures._disclosure(),), 2, True), fixtures.NOW)
    with pytest.raises(InterruptedError, match="company_sync_cancelled"):
        with index._connect() as connection, SqliteTransaction(connection):
            connection.run("UPDATE partitions SET next_page=999")
            cancelled.set()
            connection.run(
                "WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<1000000) SELECT sum(x) FROM n"
            ).fetchall()
    with SqliteSession(index.path) as connection:
        assert tuple(connection.run("SELECT next_page,received_rows,failure FROM partitions").require_one()) == (
            2,
            1,
            "",
        )
        assert connection.run("SELECT count(*) FROM disclosures").require_one()[0] == 1
    # A new invocation can continue from the committed cursor.
    cancelled.clear()
    resumed = index.partitions(("sse",), fixtures.NOW)
    assert next(item for item in resumed if item.start == day).page == 2


def test_cancelled_publication_keeps_previous_projection(tmp_path, monkeypatch):
    from trader.download.infra import company_evidence_index as publication

    cancelled = Event()
    index = SQLiteCompanyEvidenceIndex(tmp_path / "evidence.sqlite3", cancel_requested=cancelled.is_set)
    index.initialize()
    index.publish(("600001",), fixtures.NOW)
    with SqliteSession(index.path) as connection:
        before = tuple(connection.run("SELECT * FROM publication").require_one())
    encode = publication.EncodeCompanyPublication

    def cancel_during_build(snapshot):
        result = encode(snapshot)
        cancelled.set()
        return result

    monkeypatch.setattr(publication, "EncodeCompanyPublication", cancel_during_build)
    with pytest.raises(InterruptedError, match="company_sync_cancelled"):
        index.publish(("600001", "600002"), fixtures.NOW)
    with SqliteSession(index.path) as connection:
        assert tuple(connection.run("SELECT * FROM publication").require_one()) == before
        assert connection.run("SELECT count(*) FROM published").require_one()[0] == 1


@pytest.mark.parametrize("during_request", (False, True))
@pytest.mark.parametrize("component", ("pledge", "news"))
def test_structured_request_receives_cancellation_and_discards_late_response(during_request, component):
    cancelled = Event()

    class Transport(Session):
        def get(self, url, **kwargs):
            assert kwargs["cancel_requested"] == cancelled.is_set
            cancelled.set()
            return super().get(url, **kwargs)

    session = Transport({})
    source = EastmoneyCompanySource(session, monotonic=lambda: 0, cancel_requested=cancelled.is_set)
    if not during_request:
        cancelled.set()
    with pytest.raises(HttpCancelled):
        if component == "pledge":
            source.pledge_day(fixtures.NOW)
        else:
            source.news("600001", observed_at=fixtures.NOW)
    assert len(session.requests) == int(during_request)


@pytest.mark.parametrize("component", ("financial", "news"))
def test_cancelled_component_does_not_persist_source_failure(tmp_path, monkeypatch, component):
    index = fixtures._index(tmp_path)
    cancelled = Event()
    failures = []
    monkeypatch.setattr(index, "component_failure", lambda *args: failures.append(args))

    class Source(fixtures.Sources):
        def fetch(self, query, **kwargs):
            if component == "financial":
                cancelled.set()
                raise HttpCancelled("http_cancelled")
            return super().fetch(query, **kwargs)

        def news(self, code, **kwargs):
            cancelled.set()
            raise HttpCancelled("http_cancelled")

    source = Source()
    result = fixtures._sync_company_evidence(
        CompanyEvidenceDependencies(index, source, source, lambda: 0, cancel_requested=cancelled.is_set),
        ("600001",),
        fixtures.NOW,
        news_codes=("600001",),
    )
    assert result.failures == ("task:cancelled",) and not failures
