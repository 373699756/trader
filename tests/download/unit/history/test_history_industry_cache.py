from tests.helpers.shared.configuration import SqlitePolicyFixture

from dataclasses import replace

from datetime import date, datetime, timedelta

import json

from zoneinfo import ZoneInfo

import pytest




from trader.download.infra.history_industry_cache import HistoryIndustryQueryCache


from trader.download.infra.baostock_industry import BaoStockIndustryClient, BaoStockIndustryRow

from trader.download.infra.current_industry_publication import CurrentIndustryCache, CurrentIndustryPublication

from trader.download.infra.industry_query_index import SQLiteIndustryQueryIndex

from trader.download.infra.industry_evidence_codec import IndustryEvidencePayload, LoadReviewedIndustryEvidence

from trader.infra.sqlite_connection import SqliteSession, SqliteTransaction

from tests.helpers.download.history.history_industry_cache import (
    DATES,
    UNIVERSE,
    _IndustrySdk,
    _fetch,
    _official_evidence,
)


def test_same_day_reuses_complete_dates_and_new_universe_is_filtered_at_read(tmp_path):
    cache = HistoryIndustryQueryCache(tmp_path / "industry_queries.sqlite3", sqlite_policy=SqlitePolicyFixture())
    sdk = _IndustrySdk()
    original = _fetch(sdk, cache)
    assert len(sdk.calls) == 4
    sdk.calls.clear()
    events = []
    assert _fetch(sdk, cache, progress=events.append) == original
    assert sdk.calls == []
    assert [event.completed_units for event in events if event.state == "completed"] == [1, 2, 3, 4]
    assert all(event.cache_hit for event in events if event.state == "completed")
    added = replace(UNIVERSE[0], code="600002")
    result = _fetch(sdk, cache, universe=(added,))
    assert {item.code for item in result} == {"600002"}
    assert sdk.calls == []


def test_failed_date_resumes_without_requerying_successful_dates(tmp_path):
    cache = HistoryIndustryQueryCache(tmp_path / "industry_queries.sqlite3", sqlite_policy=SqlitePolicyFixture())
    sdk = _IndustrySdk()
    sdk.fail_on = DATES[2].isoformat()
    with pytest.raises(RuntimeError, match="supplier_industry_timeout"):
        _fetch(sdk, cache)
    sdk.fail_on = None
    sdk.calls.clear()
    _fetch(sdk, cache)
    assert sdk.calls == [str(day) for day in DATES[2:]]


def test_calendar_roll_keeps_historical_queries_and_only_fetches_new_dates(tmp_path):
    cache = HistoryIndustryQueryCache(tmp_path / "industry_queries.sqlite3", sqlite_policy=SqlitePolicyFixture())
    sdk = _IndustrySdk()
    _fetch(sdk, cache)
    sdk.calls.clear()
    dates = (date(2020, 2, 4), *DATES[1:-1], date(2026, 10, 12))
    _fetch(sdk, cache, dates)
    assert sdk.calls == ["2020-02-04", "2026-10-12"]
    sdk.calls.clear()
    _fetch(sdk, cache, (*dates[:-1], date(2026, 11, 12)))
    assert sdk.calls == ["2026-11-12"]
    sdk.calls.clear()
    assert _fetch(sdk, cache)  # The old rolling-window dates remain available.
    assert sdk.calls == []


@pytest.mark.parametrize("kind", ("empty", "future"))
def test_empty_or_future_only_responses_are_not_cached(tmp_path, kind):
    cache = HistoryIndustryQueryCache(tmp_path / "industry_queries.sqlite3", sqlite_policy=SqlitePolicyFixture())
    sdk = _IndustrySdk()
    setattr(sdk, kind, True)
    assert _fetch(sdk, cache) == ()
    sdk.calls.clear()
    assert _fetch(sdk, cache) == ()
    assert len(sdk.calls) == 4


def test_corrupt_cached_payload_requeries_only_that_date_and_source_change_invalidates(tmp_path):
    cache = HistoryIndustryQueryCache(tmp_path / "industry_queries.sqlite3", sqlite_policy=SqlitePolicyFixture())
    sdk = _IndustrySdk()
    original = _fetch(sdk, cache)
    with SqliteSession(cache.path) as connection, SqliteTransaction(connection):
        connection.run("UPDATE industry_query SET members_json='[]' WHERE anchor_date=?", (str(DATES[1]),))
    sdk.calls.clear()
    assert _fetch(sdk, cache) == original
    assert sdk.calls == [str(DATES[1])]
    sdk.__version__ = "changed"
    sdk.calls.clear()
    _fetch(sdk, cache)
    assert len(sdk.calls) == 4


def test_pagination_failure_never_creates_a_complete_query(tmp_path):
    cache = HistoryIndustryQueryCache(tmp_path / "industry_queries.sqlite3", sqlite_policy=SqlitePolicyFixture())

    class BrokenPage(_IndustrySdk):
        def query_stock_industry(self, **kwargs):
            result = super().query_stock_industry(**kwargs)

            def fail_next():
                raise RuntimeError("page_failed")

            result.next = fail_next
            return result

    with pytest.raises(RuntimeError, match="page_failed"):
        _fetch(BrokenPage(), cache)
    sdk = _IndustrySdk()
    _fetch(sdk, cache)
    assert len(sdk.calls) == 4


def test_recent_fixed_anchor_is_reused_when_cutoff_advances(tmp_path):
    cache = HistoryIndustryQueryCache(tmp_path / "industry_queries.sqlite3", sqlite_policy=SqlitePolicyFixture())
    sdk = _IndustrySdk()
    dates = (date(2026, 7, 1), date(2026, 10, 9))
    _fetch(sdk, cache, dates)
    sdk.calls.clear()
    _fetch(sdk, cache, (dates[0], dates[-1] + timedelta(days=3)))
    assert sdk.calls == ["2026-10-12"]
    sdk.calls.clear()
    _fetch(sdk, cache, (dates[0], dates[-1] + timedelta(days=3)))
    assert sdk.calls == []


def test_unreadable_sqlite_cache_has_explicit_failure_and_is_not_deleted(tmp_path):
    cache = HistoryIndustryQueryCache(tmp_path / "industry_queries.sqlite3", sqlite_policy=SqlitePolicyFixture())
    cache.path.write_bytes(b"not a database")
    with pytest.raises(RuntimeError, match="industry_query_cache_unavailable"):
        _fetch(_IndustrySdk(), cache)
    assert cache.path.read_bytes() == b"not a database"


def test_missing_listed_security_reuses_full_market_query_without_inventing_industry(tmp_path):
    cache = HistoryIndustryQueryCache(tmp_path / "industry_queries.sqlite3", sqlite_policy=SqlitePolicyFixture())
    sdk = _IndustrySdk()
    _fetch(sdk, cache)
    sdk.calls.clear()
    missing = replace(UNIVERSE[0], code="600003", listed_on=DATES[-1])
    for _ in range(3):
        events = []
        result = _fetch(sdk, cache, universe=(*UNIVERSE, missing), progress=events.append)
        assert sdk.calls == []
        assert {item.code for item in result} == {UNIVERSE[0].code}
        assert events[-1].industry_missing_codes == 1
        assert events[-1].cache_hit is True


@pytest.mark.parametrize("day,expected", ((1, False), (2, False), (3, True), (5, True), (6, False)))
def test_official_industry_fills_only_dated_missing_category_and_preserves_provenance(tmp_path, day, expected):
    evidence = _official_evidence()
    index = SQLiteIndustryQueryIndex(tmp_path / "industry.sqlite3", "baostock_dated_industry_queries", sqlite_policy=SqlitePolicyFixture())
    assert index.import_evidence((evidence,)) == 1
    assert index.import_evidence((evidence,)) == 0
    at = datetime(2026, 11, day, 19, tzinfo=ZoneInfo("Asia/Shanghai"))
    client = BaoStockIndustryClient(
        fetch_rows=lambda date, timeout: (BaoStockIndustryRow("sh.600001", "I65软件", "证监会行业分类", date),),
        minimum_rows=1,
    )
    rows = CurrentIndustryPublication(index.path, client, minimum_rows=1, sqlite_policy=SqlitePolicyFixture()).fetch(at)
    assert {row.subject_key for row in rows} == ({"600001", "600003"} if expected else {"600001"})
    assert rows == CurrentIndustryCache(index.path, minimum_rows=1, sqlite_policy=SqlitePolicyFixture()).load(at)
    if expected:
        official = next(row for row in rows if row.subject_key == "600003")
        assert official.source == official.fields["model_industry_source"] == "official_disclosure"
        assert official.fields["model_industry"] == "C29Plastics"
        assert official.fields["industry_native_classification"] == "CAPCO2023"
        assert official.fields["industry_evidence_id"] == evidence.identity
        assert official.source_time == evidence.disclosed_at
        assert official.payload_hash == evidence.document_sha256


def test_current_industry_cache_rejects_expired_or_future_received_official_evidence(tmp_path):
    evidence = replace(_official_evidence(), received_at=datetime(2026, 11, 4, 18, tzinfo=ZoneInfo("Asia/Shanghai")))
    index = SQLiteIndustryQueryIndex(tmp_path / "industry.sqlite3", "baostock_dated_industry_queries", sqlite_policy=SqlitePolicyFixture())
    index.import_evidence((evidence,))
    client = BaoStockIndustryClient(
        fetch_rows=lambda day, timeout: (BaoStockIndustryRow("sh.600001", "I65软件", "证监会行业分类", day),),
        minimum_rows=1,
    )
    at = evidence.received_at + timedelta(hours=1)
    source = CurrentIndustryPublication(index.path, client, minimum_rows=1, sqlite_policy=SqlitePolicyFixture())
    assert len(source.fetch(at)) == 2
    cache = CurrentIndustryCache(index.path, minimum_rows=1, sqlite_policy=SqlitePolicyFixture())
    assert cache.load(evidence.received_at - timedelta(hours=1)) == ()
    assert cache.load(at + timedelta(days=2)) == ()


def test_official_industry_never_replaces_existing_supplier_or_changes_old_publication_on_import(tmp_path):
    index = SQLiteIndustryQueryIndex(tmp_path / "industry.sqlite3", "baostock_dated_industry_queries", sqlite_policy=SqlitePolicyFixture())
    at = datetime(2026, 11, 4, 19, tzinfo=ZoneInfo("Asia/Shanghai"))
    client = BaoStockIndustryClient(
        fetch_rows=lambda day, timeout: (BaoStockIndustryRow("sh.600003", "I65软件", "证监会行业分类", day),),
        minimum_rows=1,
    )
    source = CurrentIndustryPublication(index.path, client, minimum_rows=1, sqlite_policy=SqlitePolicyFixture())
    before = source.fetch(at)
    index.import_evidence((_official_evidence(),))
    assert CurrentIndustryCache(index.path, minimum_rows=1, sqlite_policy=SqlitePolicyFixture()).load(at) == before
    assert source.fetch(at) == before
    assert client.health().planned_count == 1


def test_industry_evidence_failure_keeps_previous_publication_after_supplier_refresh(tmp_path, monkeypatch):
    path = tmp_path / "industry.sqlite3"
    at = datetime(2026, 11, 4, 19, tzinfo=ZoneInfo("Asia/Shanghai"))
    client = BaoStockIndustryClient(
        fetch_rows=lambda day, timeout: (BaoStockIndustryRow("sh.600001", "I65软件", "证监会行业分类", day),),
        minimum_rows=1,
    )
    source = CurrentIndustryPublication(path, client, minimum_rows=1, sqlite_policy=SqlitePolicyFixture())
    original = source.fetch(at - timedelta(days=7))

    def fail(*args):
        raise ValueError("industry_evidence_hash_conflict")

    monkeypatch.setattr(SQLiteIndustryQueryIndex, "read_evidence", fail)
    with pytest.raises(ValueError, match="hash_conflict"):
        source.fetch(at)
    assert client.health().planned_count == 2
    assert CurrentIndustryCache(path, minimum_rows=1, sqlite_policy=SqlitePolicyFixture()).load(at) == original


def test_industry_evidence_conflicts_are_atomic_and_corruption_is_rejected(tmp_path):
    index = SQLiteIndustryQueryIndex(tmp_path / "industry.sqlite3", "baostock_dated_industry_queries", sqlite_policy=SqlitePolicyFixture())
    original = _official_evidence()
    index.import_evidence((original,))
    conflict = replace(original, mapping=replace(original.mapping, target_industry="C29Other"))
    with pytest.raises(ValueError, match="interval_conflict"):
        index.import_evidence((replace(original, code="600004"), conflict))
    at = original.received_at + timedelta(days=2)
    assert index.read_evidence(at) == (original,)
    with SqliteSession(index.path) as connection, SqliteTransaction(connection):
        connection.run("UPDATE official_industry_evidence SET code='600005'")
    with pytest.raises(ValueError, match="hash_conflict"):
        index.read_evidence(at)


def test_explicit_writer_upgrades_industry_schema_without_rewriting_supplier_rows(tmp_path):
    index = SQLiteIndustryQueryIndex(tmp_path / "industry.sqlite3", "baostock_dated_industry_queries", sqlite_policy=SqlitePolicyFixture())
    index.save("supplier", DATES[0], DATES[0], "[]")
    with SqliteSession(index.path) as connection, SqliteTransaction(connection):
        before = tuple(connection.run("SELECT * FROM industry_query").require_one())
        connection.run("DROP TABLE official_industry_evidence")
        connection.run("PRAGMA user_version=1")
    with pytest.raises(RuntimeError, match="unavailable"):
        index.read(DATES[0], DATES[0])
    index.import_evidence((_official_evidence(),))
    with SqliteSession(index.path) as connection:
        assert tuple(connection.run("SELECT * FROM industry_query").require_one()) == before
        assert connection.run("PRAGMA user_version").require_one()[0] == 1


@pytest.mark.parametrize("cancelled", (True, False))
def test_reviewed_industry_import_rejects_future_receipt_and_cancellation_before_database_write(tmp_path, monkeypatch, cancelled):
    import trader.download.infra.industry_query_index as module

    index = SQLiteIndustryQueryIndex(tmp_path / "industry.sqlite3", "baostock_dated_industry_queries", sqlite_policy=SqlitePolicyFixture())
    evidence = _official_evidence()
    monkeypatch.setattr(module, "LoadReviewedIndustryEvidence", lambda _: (evidence,))
    if cancelled:
        assert not index.import_reviewed_file(tmp_path / "input.json", observed_at=evidence.disclosed_at, cancelled=lambda: True)
    else:
        with pytest.raises(ValueError, match="received_in_future"):
            index.import_reviewed_file(tmp_path / "input.json", observed_at=evidence.disclosed_at, cancelled=lambda: False)
    assert not index.path.exists()


@pytest.mark.parametrize("invalid", ("issuer", "excerpt", "mapping", "path", "url", "valid"))
def test_reviewed_import_rejects_wrong_subject_or_missing_document_evidence(tmp_path, monkeypatch, invalid):
    import trader.download.infra.industry_evidence_codec as codec

    class _Page:
        def __init__(self, text):
            self.text = text

        def extract_text(self):
            return self.text

    class _Pdf:
        def __init__(self, text):
            self.pages = (_Page(text),)
            self.closed = False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.closed = True

    source = _Pdf("股票代码：600003 CAPCO2023 C29 Plastics")
    mapping = _Pdf("CSRC2012 C29Plastics")
    evidence = _official_evidence()
    raw = IndustryEvidencePayload(evidence)
    if invalid == "issuer":
        source.pages[0].text = "股票代码：600009 CAPCO2023 C29 Plastics"
    elif invalid == "excerpt":
        source.pages[0].text = "股票代码：600003 missing"
    elif invalid == "mapping":
        mapping.pages[0].text = "C29Other"
    elif invalid == "url":
        raw["source_url"] = "https://example.com/unofficial.pdf"
    monkeypatch.setattr(codec, "_pdf", lambda root, name, digest: source if name == "issuer.pdf" else mapping)
    manifest = [{"evidence": raw, "document": "issuer.pdf", "mapping_document": "mapping.pdf"}]
    path = tmp_path / "evidence.json"
    if invalid == "path":
        monkeypatch.undo()
        manifest[0]["document"] = "../outside.pdf"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    if invalid == "valid":
        assert LoadReviewedIndustryEvidence(path) == (evidence,)
    else:
        with pytest.raises(ValueError):
            LoadReviewedIndustryEvidence(path)
    if invalid not in {"path", "url"}:
        assert source.closed and mapping.closed
