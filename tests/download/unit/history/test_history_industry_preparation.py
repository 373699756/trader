import json
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from tests.helpers.shared.configuration import SqlitePolicyFixture
from trader.download.domain.baostock_daily import BaoStockSecurity
from trader.download.domain.history_sync import HistorySyncProgress
from trader.download.infra.cninfo_industry import CninfoIndustryClient, CninfoIndustryError
from trader.download.infra.history_industry_preparation import InspectHistoricalIndustryQueries
from trader.download.infra.industry_query_index import IndustrySourceDocument, SQLiteIndustryQueryIndex
from trader.infra.sqlite_connection import SqliteSession, SqliteTransaction

_OBSERVED_AT = datetime(2026, 11, 6, 20, tzinfo=ZoneInfo("Asia/Shanghai"))
_FIRST_DATE = date(2020, 1, 1)
_LAST_DATE = date(2026, 11, 6)
_UNIVERSE = tuple(
    BaoStockSecurity(code, "测试股份", "main", date(2000, 1, 1), None, "fixture")
    for code in ("600001", "600002")
)


@dataclass
class _Progress:
    values: list[HistorySyncProgress] = field(default_factory=list)

    def publish(self, progress: HistorySyncProgress) -> None:
        self.values.append(progress)


def _index(root):
    return SQLiteIndustryQueryIndex(root / "industry.sqlite3", "cninfo_industry_queries", sqlite_policy=SqlitePolicyFixture())


def _document(code="600001", *, standard="中国上市公司协会上市公司行业分类标准"):
    record = {
        "SECCODE": code, "SECNAME": "测试股份", "ORGNAME": "测试股份有限公司", "VARYDATE": "2024-09-30",
        "F001V": "008001", "F002V": standard, "F003V": "C243", "F004V": "制造业", "F005V": "造纸、印刷、文教",
        "F006V": "文教、工美、体育和娱乐用品制造业", "F007V": "工艺美术及礼仪用品制造", "F008C": "1",
    }
    content = json.dumps({"resultcode": 200, "count": 1, "records": [record]}, ensure_ascii=False).encode()
    return IndustrySourceDocument("cninfo:p_stock2110:" + code, _FIRST_DATE, _LAST_DATE, _OBSERVED_AT, content)


def _inspect(index, *, universe=_UNIVERSE, observed_at=_OBSERVED_AT, cancel_requested=lambda: False, progress=None):
    return InspectHistoricalIndustryQueries(
        index, universe, _FIRST_DATE, _LAST_DATE, observed_at, cancel_requested=cancel_requested, progress=progress
    )


def test_absent_source_database_is_not_created_and_history_remains_unqualified(tmp_path):
    index = _index(tmp_path / "missing")
    readiness = _inspect(index)
    assert (readiness.requested_codes, readiness.candidate_codes, readiness.point_in_time_codes) == (2, 0, 0)
    assert readiness.source == "cninfo" and not readiness.ready
    assert not index.path.parent.exists()
    assert _inspect(None) == readiness


def test_full_current_standard_candidate_coverage_never_becomes_point_in_time_intervals(tmp_path, monkeypatch):
    index = _index(tmp_path)
    originals = tuple(_document(security.code) for security in _UNIVERSE)
    for original in originals:
        index.save_source_document(original)

    def unexpected(*args, **kwargs):
        pytest.fail("history inspection must not fetch industry or taxonomy")

    monkeypatch.setattr(CninfoIndustryClient, "fetch_changes", unexpected)
    monkeypatch.setattr(CninfoIndustryClient, "fetch_taxonomy", unexpected)
    progress = _Progress()
    readiness = _inspect(index, progress=progress)
    assert (readiness.requested_codes, readiness.candidate_codes, readiness.point_in_time_codes) == (2, 2, 0)
    assert not readiness.ready
    assert [item.current_item for item in progress.values] == [security.code for security in _UNIVERSE]
    assert [item.completed_units for item in progress.values] == [1, 2]
    assert [item.industry_missing_codes for item in progress.values] == [1, 2]
    assert all(item.cache_hit for item in progress.values)
    for original in originals:
        assert index.read_source_document(original.request_key, _OBSERVED_AT) == original


def test_partial_candidate_cache_preserves_missing_stock_as_missing(tmp_path):
    index = _index(tmp_path)
    index.save_source_document(_document())
    progress = _Progress()
    readiness = _inspect(index, progress=progress)
    assert (readiness.candidate_codes, readiness.point_in_time_codes) == (1, 0)
    assert [item.cache_hit for item in progress.values] == [True, None]
    assert not readiness.ready


@pytest.mark.parametrize("fields", (
    {"requested_from": _FIRST_DATE + timedelta(days=1)},
    {"requested_to": _LAST_DATE - timedelta(days=1)},
))
def test_partial_date_window_is_not_full_historical_candidate_coverage(tmp_path, fields):
    index = _index(tmp_path)
    index.save_source_document(replace(_document(), **fields))
    readiness = _inspect(index, universe=_UNIVERSE[:1])
    assert (readiness.candidate_codes, readiness.point_in_time_codes) == (0, 0)


def test_candidate_window_starts_at_listing_without_requiring_pre_listing_dates(tmp_path):
    index = _index(tmp_path)
    security = replace(_UNIVERSE[0], listed_on=date(2024, 1, 1))
    index.save_source_document(replace(_document(), requested_from=security.listed_on))
    readiness = _inspect(index, universe=(security,))
    assert (readiness.candidate_codes, readiness.point_in_time_codes) == (1, 0)
    assert not readiness.ready


def test_old_standard_originals_are_retained_without_counting_as_current_candidates(tmp_path):
    index = _index(tmp_path)
    old = _document(standard="证监会行业分类2012")
    index.save_source_document(old)
    readiness = _inspect(index, universe=_UNIVERSE[:1])
    assert (readiness.candidate_codes, readiness.point_in_time_codes) == (0, 0)
    assert index.read_source_document(old.request_key, _OBSERVED_AT) == old


def test_receipt_from_the_future_does_not_make_an_earlier_observation_ready(tmp_path):
    index = _index(tmp_path)
    index.save_source_document(_document())
    readiness = _inspect(index, observed_at=_OBSERVED_AT - timedelta(seconds=1))
    assert (readiness.candidate_codes, readiness.point_in_time_codes) == (0, 0)


def test_persisted_response_is_reparsed_instead_of_trusting_a_matching_raw_digest(tmp_path):
    index = _index(tmp_path)
    invalid = replace(_document(), content=b'{"resultcode":200,"count":1,"records":[]}')
    index.save_source_document(invalid)
    with pytest.raises(CninfoIndustryError, match="records_incomplete"):
        _inspect(index)
    assert index.read_source_document(invalid.request_key, _OBSERVED_AT) == invalid


def test_persisted_response_digest_conflict_is_not_silently_skipped_or_repaired(tmp_path):
    index = _index(tmp_path)
    index.save_source_document(_document())
    with SqliteSession(index.path) as connection, SqliteTransaction(connection):
        connection.run("UPDATE industry_source_document SET payload=?", (b"altered",))
    before = index.path.read_bytes()
    with pytest.raises(ValueError, match="hash_conflict"):
        _inspect(index)
    assert index.path.read_bytes() == before


def test_cancellation_stops_before_the_first_cache_read_or_fetch(tmp_path, monkeypatch):
    index = _index(tmp_path)

    def unexpected(*args, **kwargs):
        pytest.fail("cancellation must be checked before source access")

    monkeypatch.setattr(SQLiteIndustryQueryIndex, "read_source_document", unexpected)
    monkeypatch.setattr(CninfoIndustryClient, "fetch_changes", unexpected)
    with pytest.raises(RuntimeError, match="supplier_cancelled"):
        _inspect(index, cancel_requested=lambda: True)
    assert not index.path.exists()


def test_cancellation_between_stocks_stops_without_touching_remaining_originals(tmp_path, monkeypatch):
    index = _index(tmp_path)
    for security in _UNIVERSE:
        index.save_source_document(_document(security.code))
    reads = []
    read = SQLiteIndustryQueryIndex.read_source_document

    def observed_read(self, request_key, as_of):
        reads.append(request_key)
        return read(self, request_key, as_of)

    monkeypatch.setattr(SQLiteIndustryQueryIndex, "read_source_document", observed_read)
    progress = _Progress()
    with pytest.raises(RuntimeError, match="supplier_cancelled"):
        _inspect(index, progress=progress, cancel_requested=lambda: bool(progress.values))
    assert reads == ["cninfo:p_stock2110:600001"]
    assert len(progress.values) == 1
    for security in _UNIVERSE:
        original = _document(security.code)
        assert read(index, original.request_key, _OBSERVED_AT) == original
