"""Training reads one fresh immutable publication and its verified original receipts."""

import json
from dataclasses import replace
from datetime import date, datetime, timedelta
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

import pytest

from tests.helpers.shared.configuration import SqlitePolicyFixture
from tests.helpers.shared.reference_fixtures import PreparedFixture
from trader.download.infra.cninfo_industry import CninfoIndustryClient
from trader.download.infra.current_industry_publication import CurrentIndustryCache, CurrentIndustryPublication
from trader.download.infra.industry_query_index import SQLiteIndustryQueryIndex
from trader.infra.http_contracts import HttpResponse
from trader.infra.http_transport import RequestsHttpTransport
from trader.infra.sqlite_connection import SqliteSession, SqliteTransaction

_AT = datetime(2026, 11, 6, 12, tzinfo=ZoneInfo("Asia/Shanghai"))
_STANDARD = "中国上市公司协会上市公司行业分类标准"


@pytest.fixture
def publication(tmp_path):
    calls = []
    now = [_AT]

    def execute(request):
        parsed = urlsplit(request.url)
        calls.append(request.url)
        if parsed.path.endswith("p_public0002"):
            records = [
                {"SORTCODE": code, "PARENTCODE": parent, "SORTNAME": name, "F001V": None, "F002D": None,
                 "F003V": "008001", "F004V": _STANDARD}
                for code, parent, name in (("0", "008", "中上协行业分类"), ("J", "0", "金融业"),
                                          ("J66", "J", "货币金融服务"), ("J661", "J66", "中央银行服务"))
            ]
        else:
            code = parse_qs(parsed.query)["scode"][0]
            records = [] if code == "600002" else [{
                "SECCODE": code, "SECNAME": "测试股份", "ORGNAME": "测试股份有限公司", "VARYDATE": "2024-09-30",
                "F001V": "008001", "F002V": _STANDARD, "F003V": "J661", "F004V": "金融业",
                "F005V": None, "F006V": "货币金融服务", "F007V": "中央银行服务", "F008C": "1",
            }]
        return HttpResponse(200, json.dumps({"resultcode": 200, "count": len(records), "records": records}, ensure_ascii=False).encode())

    with RequestsHttpTransport(execute_request=execute) as transport:
        source = CurrentIndustryPublication(
            tmp_path / "industry.sqlite3",
            CninfoIndustryClient(transport, request_headers=lambda: (("Accept-Enckey", "fixture"),)),
            sqlite_policy=SqlitePolicyFixture(),
            clock=lambda: now[0],
        )
        security = PreparedFixture(_AT).official_universe[0]
        universe = tuple(replace(security, code=code) for code in ("600001", "600002"))
        source.fetch(_AT, universe, (date(2024, 1, 1), _AT.date()))
        yield source, universe, now, calls


def _cache(source):
    return CurrentIndustryCache(source.path, sqlite_policy=source.sqlite_policy)


def _index(source):
    return SQLiteIndustryQueryIndex(source.path, "cninfo_industry_queries", sqlite_policy=source.sqlite_policy)


def test_snapshot_uses_publication_time_native_name_and_verified_gap_without_network(publication):
    source, _universe, _now, calls = publication
    before = source.path.read_bytes()
    calls.clear()
    snapshot = _cache(source).snapshot(_AT + timedelta(days=6))
    assert snapshot.observed_at == _AT
    assert snapshot.refresh_after == _AT + timedelta(days=7)
    assert snapshot.publication_hash == _index(source).latest(_AT.date()).digest
    assert snapshot.members[0].reference.industry_id == "capco:J66货币金融服务"
    assert snapshot.members[0].name == "货币金融服务"
    assert snapshot.gaps[0].code == "600002"
    assert snapshot.gaps[0].reason == "industry_records_empty"
    assert snapshot.project(("600001", "600003")).gaps[0].reason == "current_industry_not_published"
    assert calls == []
    assert source.path.read_bytes() == before


def test_snapshot_refuses_absent_or_expired_publication_without_network(publication, tmp_path):
    source, _universe, _now, calls = publication
    missing = CurrentIndustryCache(tmp_path / "missing.sqlite3", sqlite_policy=source.sqlite_policy)
    with pytest.raises(ValueError, match="current_industry_not_published"):
        missing.snapshot(_AT)
    assert not missing.path.exists()
    calls.clear()
    with pytest.raises(ValueError, match="current_industry_publication_expired"):
        _cache(source).snapshot(_AT + timedelta(days=7))
    assert calls == []


def test_refresh_changes_evidence_but_not_semantic_grouping_identity(publication):
    source, universe, now, _calls = publication
    before = _cache(source).snapshot(_AT).project(("600001",))
    now[0] += timedelta(days=7)
    source.fetch(now[0], universe, (date(2024, 1, 1), now[0].date()))
    after = _cache(source).snapshot(now[0]).project(("600001",))
    assert after.content_hash == before.content_hash
    assert after.publication_hash != before.publication_hash
    assert after.observed_at != before.observed_at


@pytest.mark.parametrize("table,assignment", (
    ("industry_source_document", "payload=x'00'"),
    ("industry_source_document", "receipt_hash=zeroblob(32)"),
))
def test_snapshot_rejects_missing_or_damaged_original_receipts(publication, table, assignment):
    source, _universe, _now, _calls = publication
    with SqliteSession(source.path) as connection, SqliteTransaction(connection):
        connection.run(f"UPDATE {table} SET {assignment} WHERE request_key='cninfo:p_stock2110:600001'")
    with pytest.raises(ValueError, match="current_industry_source_evidence_invalid"):
        _cache(source).snapshot(_AT)


@pytest.mark.parametrize("damage", ("digest", "structure", "checked_date"))
def test_strict_latest_rejects_bad_newest_publication_instead_of_using_older_good_record(publication, damage):
    source, universe, now, _calls = publication
    index = _index(source)
    old = index.latest(_AT.date())
    now[0] += timedelta(days=1)
    source.fetch(now[0], (*universe, replace(universe[0], code="600003")), (date(2024, 1, 1), now[0].date()))
    if damage in ("digest", "checked_date"):
        with SqliteSession(source.path) as connection, SqliteTransaction(connection):
            assignment = "content_hash=zeroblob(32)" if damage == "digest" else "checked_date='invalid'"
            connection.run(f"UPDATE current_industry_query SET {assignment} WHERE as_of_date=?", (now[0].date().isoformat(),))
        assert index.latest(now[0].date()) == old
        reason = "industry_query_publication_invalid"
    else:
        index.save("cninfo_industry", now[0].date(), now[0].date(), json.dumps({"rows": []}))
        reason = "current_industry_payload_incompatible"
    with pytest.raises(ValueError, match=reason):
        _cache(source).snapshot(now[0])
