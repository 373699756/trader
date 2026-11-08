"""CNINFO current publications retain evidence and never dispatch excluded stocks."""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

import pytest

from tests.helpers.shared.configuration import SqlitePolicyFixture
from tests.helpers.shared.reference_fixtures import PreparedFixture
from trader.download.infra.cninfo_industry import CninfoIndustryClient
from trader.download.infra.current_industry_publication import CurrentIndustryCache, CurrentIndustryPublication
from trader.download.infra.industry_query_index import SQLiteIndustryQueryIndex
from trader.infra.http_contracts import HttpCancelled, HttpResponse, HttpTimeout
from trader.infra.http_transport import RequestsHttpTransport
from trader.infra.sqlite_connection import SqliteSession, SqliteTransaction
from trader.recommendation.domain.market.models import ModelIndustryReference

_AT = datetime(2026, 11, 6, 12, tzinfo=ZoneInfo("Asia/Shanghai"))
_START = date(2024, 1, 1)
_STANDARD = "中国上市公司协会上市公司行业分类标准"


def _response(records):
    return HttpResponse(200, json.dumps({"resultcode": 200, "count": len(records), "records": records}, ensure_ascii=False).encode())


def _taxonomy():
    return _response([
        {"SORTCODE": code, "PARENTCODE": parent, "SORTNAME": name, "F001V": None, "F002D": None,
         "F003V": "008001", "F004V": _STANDARD}
        for code, parent, name in (("0", "008", "中上协行业分类"), ("J", "0", "金融业"),
                                   ("J66", "J", "货币金融服务"), ("J661", "J66", "中央银行服务"))
    ])


def _change(code, *, standard=_STANDARD):
    return {"SECCODE": code, "SECNAME": "测试股份", "ORGNAME": "测试股份有限公司", "VARYDATE": "2024-09-30",
            "F001V": "008001", "F002V": standard, "F003V": "J661", "F004V": "金融业",
            "F005V": None, "F006V": "货币金融服务", "F007V": "中央银行服务", "F008C": "1"}


@pytest.fixture
def fixture(tmp_path):
    now, calls, outcomes = [_AT], [], {}

    def execute(request):
        query = parse_qs(urlsplit(request.url).query)
        endpoint = urlsplit(request.url).path.rsplit("/", 1)[-1]
        code = query.get("scode", [""])[0]
        calls.append((endpoint, code, query))
        result = outcomes.get(code)
        if isinstance(result, Exception):
            raise result
        if endpoint == "p_public0002":
            return _taxonomy()
        return _response([_change(code)] if result is None else result)

    with RequestsHttpTransport(execute_request=execute) as transport:
        client = CninfoIndustryClient(transport, request_headers=lambda: (("Accept-Enckey", "fixture"),))
        source = CurrentIndustryPublication(
            tmp_path / "industry.sqlite3", client, sqlite_policy=SqlitePolicyFixture(), clock=lambda: now[0],
            source_batch_rows=64,
        )
        yield source, now, calls, outcomes


def _universe(*codes):
    from dataclasses import replace

    security = PreparedFixture(_AT).official_universe[0]
    return tuple(replace(security, code=code) for code in sorted(codes or ("600001",)))


def _fetch(source, at=_AT, codes=("600001",), start=_START):
    return source.fetch(at, _universe(*codes), (start, at.date()))


def test_current_industry_follows_taxonomy_parent_and_retains_original_evidence(fixture):
    source, now, calls, _outcomes = fixture
    rows = _fetch(source)
    assert len(rows) == 1
    row = rows[0]
    assert row.source == row.fields["model_industry_source"] == "cninfo_industry"
    assert row.fields["model_industry"] == "capco:J66货币金融服务"
    assert row.fields["model_industry_classification"] == _STANDARD
    assert row.fields["industry_native_code"] == "J661"
    assert row.fields["industry_latest_marker"] == "1"
    assert row.source_time == row.received_at == now[0]
    assert row.effective_at.date() == date(2024, 9, 30)
    assert len(row.payload_hash) == 64
    assert [endpoint for endpoint, _code, _query in calls] == ["p_public0002", "p_stock2110"]
    cache = CurrentIndustryCache(source.path, sqlite_policy=source.sqlite_policy)
    assert cache.load(_AT) == rows
    index = SQLiteIndustryQueryIndex(source.path, "cninfo_industry_queries", sqlite_policy=source.sqlite_policy)
    document = index.read_source_document("cninfo:p_stock2110:600001", _AT)
    assert document is not None and hashlib.sha256(document.content).hexdigest() == row.payload_hash


def test_seven_day_reuse_preserves_source_age_and_refreshes_when_due(fixture):
    source, now, calls, _outcomes = fixture
    first = _fetch(source)
    now[0] += timedelta(days=6)
    assert _fetch(source, now[0]) == first
    assert len(calls) == 2
    now[0] += timedelta(days=1)
    refreshed = _fetch(source, now[0])
    assert len(calls) == 4
    assert refreshed[0].received_at == now[0]
    assert refreshed[0].data_version != first[0].data_version


def test_same_day_population_addition_only_fetches_new_security(fixture):
    source, _now, calls, _outcomes = fixture
    _fetch(source)
    calls.clear()
    result = _fetch(source, codes=("600001", "600002"))
    assert tuple(row.subject_key for row in result) == ("600001", "600002")
    assert [(endpoint, code) for endpoint, code, _query in calls] == [("p_stock2110", "600002")]


def test_refresh_never_narrows_previously_cached_window(fixture):
    source, now, calls, _outcomes = fixture
    _fetch(source)
    now[0] += timedelta(days=7)
    _fetch(source, now[0], start=date(2024, 6, 1))
    assert calls[-1][2]["sdate"] == [_START.isoformat()]


@pytest.mark.parametrize("rows,reason", (([], "industry_records_empty"), ([_change("600002", standard="证监会行业分类2012")], "current_standard_absent")))
def test_missing_or_unsupported_current_classification_is_an_explicit_gap(fixture, rows, reason):
    source, _now, _calls, outcomes = fixture
    outcomes["600002"] = rows
    result = _fetch(source, codes=("600001", "600002"))
    assert tuple(row.subject_key for row in result) == ("600001",)
    index = SQLiteIndustryQueryIndex(source.path, "cninfo_industry_queries", "current_industry_query", sqlite_policy=source.sqlite_policy)
    query = index.latest(_AT.date())
    assert query is not None
    assert json.loads(query.payload)["gaps"] == [{"code": "600002", "reason": reason}]


def test_empty_response_is_cached_but_never_published_as_a_classification(fixture):
    source, _now, calls, outcomes = fixture
    outcomes["600001"] = []
    with pytest.raises(ValueError, match="incomplete"):
        _fetch(source)
    calls.clear()
    with pytest.raises(ValueError, match="incomplete"):
        _fetch(source)
    assert calls == []
    assert CurrentIndustryCache(source.path, sqlite_policy=source.sqlite_policy).load(_AT) == ()


def test_failed_refresh_keeps_previous_publication_and_finished_code_documents(fixture):
    source, now, calls, outcomes = fixture
    before = _fetch(source)
    now[0] += timedelta(days=7)
    outcomes["600002"] = HttpTimeout("fixture")
    with pytest.raises(HttpTimeout):
        _fetch(source, now[0], codes=("600001", "600002"))
    assert CurrentIndustryCache(source.path, sqlite_policy=source.sqlite_policy).load(now[0]) == before
    outcomes.clear()
    calls.clear()
    _fetch(source, now[0], codes=("600001", "600002"))
    assert [(endpoint, code) for endpoint, code, _query in calls] == [("p_stock2110", "600002")]


def test_cancelled_preparation_never_publishes_or_requests(fixture):
    from dataclasses import replace

    source, _now, calls, _outcomes = fixture
    cancelled = replace(source, cancel_requested=lambda: True)
    with pytest.raises(HttpCancelled):
        _fetch(cancelled)
    assert calls == []


def test_per_stock_progress_reports_request_cache_and_missing_classification(fixture):
    from dataclasses import replace

    from trader.download.application.progress_message import ProgressLevel

    source, _now, _calls, outcomes = fixture
    _fetch(source)
    outcomes["600002"] = []
    messages = []
    source = replace(source, report=messages.append)
    _fetch(source, codes=("600001", "600002"))
    messages = [message for message in messages if message.fields[0].text.split("/", 1)[0].isdigit()]
    assert [message.fields[0].text for message in messages] == ["1/2股", "2/2股"]
    assert messages[0].fields[1].text == "代码 600001"
    assert messages[0].fields[2].text == "缓存复用"
    assert any(field.text == "本地复用（原来源 巨潮）" for field in messages[0].fields)
    assert messages[1].fields[2].text == "接口获取"
    assert messages[1].fields[3].level is ProgressLevel.WARNING
    assert messages[1].fields[4].text == "原件缓存未复用 无原件"
    assert any(field.text == "来源 巨潮" for field in messages[1].fields)


def test_late_received_time_is_preserved_instead_of_replaced_by_query_start(fixture):
    source, now, _calls, _outcomes = fixture
    now[0] += timedelta(seconds=8)
    result = _fetch(source)
    assert result[0].received_at == now[0]
    assert CurrentIndustryCache(source.path, sqlite_policy=source.sqlite_policy).load(_AT) == ()
    assert CurrentIndustryCache(source.path, sqlite_policy=source.sqlite_policy).load(now[0]) == result


def test_current_refresh_does_not_write_history_queries(fixture):
    source, _now, _calls, _outcomes = fixture
    _fetch(source)
    with SqliteSession(source.path) as connection:
        assert connection.run("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='industry_query'").require_one()[0] == 0


def test_current_publication_does_not_adopt_a_replaced_native_record(fixture):
    source, _now, _calls, outcomes = fixture
    outcomes["600001"] = [{**_change("600001"), "F008C": "0"}]
    rows = source.fetch(_AT, _universe("600001", "600002"), (_START, _AT.date()))
    assert tuple(row.subject_key for row in rows) == ("600002",)
    index = SQLiteIndustryQueryIndex(source.path, "cninfo_industry_queries", sqlite_policy=source.sqlite_policy)
    query = index.latest(_AT.date())
    assert query is not None
    assert json.loads(query.payload)["gaps"] == [{"code": "600001", "reason": "current_latest_record_absent"}]


def test_corrupt_current_query_is_rejected_without_removing_source_material(fixture):
    source, _now, _calls, _outcomes = fixture
    _fetch(source)
    with SqliteSession(source.path) as connection, SqliteTransaction(connection):
        connection.run("UPDATE current_industry_query SET members_json='[]'")
    assert CurrentIndustryCache(source.path, sqlite_policy=source.sqlite_policy).load(_AT) == ()
    with SqliteSession(source.path) as connection:
        assert connection.run("SELECT COUNT(*) FROM industry_source_document").require_one()[0] == 2


def test_old_four_column_current_payload_is_explicitly_incompatible(fixture):
    from trader.download.infra.current_industry_publication import CurrentIndustryDatabaseIncompatibleError

    source, _now, _calls, _outcomes = fixture
    _fetch(source)
    index = SQLiteIndustryQueryIndex(source.path, "cninfo_industry_queries", "current_industry_query", sqlite_policy=source.sqlite_policy)
    index.save("cninfo_industry", _AT.date(), _AT.date(), json.dumps([["600001", "2024-09-30", "J66", "证监会行业分类"]]))
    with pytest.raises(CurrentIndustryDatabaseIncompatibleError):
        CurrentIndustryCache(source.path, sqlite_policy=source.sqlite_policy).load(_AT)


@pytest.mark.parametrize("invalid", ("refresh_after", "late_row", "late_taxonomy"))
def test_current_codec_rejects_corrupt_publication_time_metadata(fixture, invalid):
    source, _now, _calls, _outcomes = fixture
    _fetch(source)
    index = SQLiteIndustryQueryIndex(source.path, "cninfo_industry_queries", "current_industry_query", sqlite_policy=source.sqlite_policy)
    query = index.latest(_AT.date())
    assert query is not None
    payload = json.loads(query.payload)
    if invalid == "refresh_after":
        payload["refresh_after"] = "not-a-time"
    elif invalid == "late_row":
        payload["rows"][0]["received_at"] = (_AT + timedelta(seconds=1)).isoformat()
    else:
        payload["taxonomy_received_at"] = (_AT + timedelta(seconds=1)).isoformat()
    index.save("cninfo_industry", _AT.date(), _AT.date(), json.dumps(payload))
    assert CurrentIndustryCache(source.path, sqlite_policy=source.sqlite_policy).load(_AT + timedelta(seconds=2)) == ()


def test_old_model_industry_identity_is_not_accepted_as_current_capco():
    with pytest.raises(ValueError, match="identity"):
        ModelIndustryReference("J66货币金融服务", _STANDARD, _AT.date(), "cninfo_industry", "fixture")
    with pytest.raises(ValueError, match="identity"):
        ModelIndustryReference("capco:J66货币金融服务", "证监会行业分类", _AT.date(), "cninfo_industry", "fixture")


def test_original_batches_are_bounded_and_progress_requires_committed_bytes(fixture, monkeypatch):
    from dataclasses import replace

    source, _now, calls, _outcomes = fixture
    source = replace(source, sqlite_policy=replace(source.sqlite_policy, transaction_batch_rows=2))
    index = SQLiteIndustryQueryIndex(source.path, "cninfo_industry_queries", sqlite_policy=source.sqlite_policy)
    save = SQLiteIndustryQueryIndex.save_source_documents
    batches = []
    completed = []

    def observed_save(self, documents):
        batches.append(tuple(document.request_key for document in documents))
        save(self, documents)

    def progress(message):
        if not message.fields[0].text.split("/", 1)[0].isdigit():
            return
        code = message.fields[1].text.removeprefix("代码 ")
        assert index.read_source_document("cninfo:p_stock2110:" + code, _AT) is not None
        completed.append(code)

    monkeypatch.setattr(SQLiteIndustryQueryIndex, "save_source_documents", observed_save)
    result = _fetch(replace(source, report=progress), codes=("600001", "600002", "600003"))
    assert [len(batch) for batch in batches] == [1, 2, 1]
    assert completed == ["600001", "600002", "600003"]
    assert [code for endpoint, code, _query in calls if endpoint == "p_stock2110"] == completed
    for row in result:
        document = index.read_source_document("cninfo:p_stock2110:" + row.subject_key, _AT)
        assert document is not None and document.content_hash == row.payload_hash
        assert json.loads(document.content)["records"] == [_change(row.subject_key)]


def test_cancellation_retains_completed_originals_without_publishing_or_requerying(fixture):
    from dataclasses import replace

    source, _now, calls, _outcomes = fixture
    cancelled = replace(source, cancel_requested=lambda: len(calls) >= 3)
    with pytest.raises(HttpCancelled):
        _fetch(cancelled, codes=("600001", "600002"))
    index = SQLiteIndustryQueryIndex(source.path, "cninfo_industry_queries", sqlite_policy=source.sqlite_policy)
    assert index.read_source_document("cninfo:p_stock2110:600001", _AT) is not None
    assert index.read_source_document("cninfo:p_stock2110:600002", _AT) is None
    assert CurrentIndustryCache(source.path, sqlite_policy=source.sqlite_policy).load(_AT) == ()
    calls.clear()
    assert len(_fetch(source, codes=("600001", "600002"))) == 2
    assert [(endpoint, code) for endpoint, code, _query in calls] == [("p_stock2110", "600002")]


def test_failed_batch_is_not_retried_or_reported_complete_by_final_flush(fixture, monkeypatch):
    from dataclasses import replace

    source, _now, _calls, _outcomes = fixture
    source = replace(source, sqlite_policy=replace(source.sqlite_policy, transaction_batch_rows=2))
    save = SQLiteIndustryQueryIndex.save_source_documents
    attempts = []
    progress = []

    def fail_stock_batch(self, documents):
        if documents[0].request_key.startswith("cninfo:p_stock2110:"):
            attempts.append(documents)
            raise RuntimeError("fixture_commit_failed")
        save(self, documents)

    monkeypatch.setattr(SQLiteIndustryQueryIndex, "save_source_documents", fail_stock_batch)
    with pytest.raises(RuntimeError, match="fixture_commit_failed"):
        _fetch(replace(source, report=progress.append), codes=("600001", "600002", "600003"))
    assert len(attempts) == 1 and len(attempts[0]) == 2
    assert not any(message.fields[0].text.split("/", 1)[0].isdigit() for message in progress)
    assert not any(message.state == "完成" for message in progress)
    index = SQLiteIndustryQueryIndex(source.path, "cninfo_industry_queries", sqlite_policy=source.sqlite_policy)
    assert index.read_source_document("cninfo:p_stock2110:600001", _AT) is None
    assert CurrentIndustryCache(source.path, sqlite_policy=source.sqlite_policy).load(_AT) == ()


def test_default_publication_keeps_single_stock_commits_until_candidate_is_accepted(fixture, monkeypatch):
    source, _now, _calls, _outcomes = fixture
    source = CurrentIndustryPublication(source.path, source.client, sqlite_policy=source.sqlite_policy, clock=source.clock)
    save = SQLiteIndustryQueryIndex.save_source_documents
    batches = []

    def observed_save(self, documents):
        batches.append(len(documents))
        save(self, documents)

    monkeypatch.setattr(SQLiteIndustryQueryIndex, "save_source_documents", observed_save)
    assert len(_fetch(source, codes=("600001", "600002"))) == 2
    assert batches == [1, 1, 1]


@pytest.mark.parametrize("rows", (0, 65, True))
def test_invalid_batch_budget_is_rejected_at_construction(fixture, rows):
    from dataclasses import replace

    source, _now, _calls, _outcomes = fixture
    with pytest.raises(ValueError, match="batch_rows_invalid"):
        replace(source, source_batch_rows=rows)


def test_feedback_reports_effective_budget_real_batches_and_exclusive_operation_totals(fixture):
    from dataclasses import replace

    source, _now, calls, _outcomes = fixture
    clock = [0.0]
    messages = []

    def monotonic():
        clock[0] += 1
        return clock[0]

    source = replace(source, monotonic=monotonic, report=messages.append)
    _fetch(source, codes=("600001", "600002"))
    first = " | ".join(field.text for field in messages[0].fields)
    assert "有效单批上限" not in first and "正文合计上限" not in first
    assert "行业归属核验范围" in first and "公共参考，非公司资料披露窗口" in first
    assert "累计" not in first and "已收到个股原件" not in first
    assert "来源 巨潮" in first
    final = " | ".join(field.text for field in messages[-1].fields)
    assert messages[-1].state == "完成"
    assert "结束诊断" in final and "有效单批上限 64股" in final
    assert "正文合计上限 2097152字节" in final
    assert "接口获取（含解析）累计 3.000秒" in final
    assert "原件保存累计 2.000秒" in final
    assert "完整发布累计 1.000秒" in final
    assert "已收到个股原件 2份" in final and "已保存个股原件 2份" in final
    assert "未保存个股原件 0份" in final and "本轮最大个股批次 2份" in final
    assert "发布缓存未复用 无完整发布" in " | ".join(field.text for message in messages for field in message.fields)

    calls.clear()
    messages.clear()
    _fetch(source, codes=("600001", "600002"))
    assert calls == []
    reused = " | ".join(field.text for field in messages[-1].fields)
    assert "当前发布缓存复用 2 股" in reused and "接口调用 0次" in reused
    assert "来源 巨潮" in reused
    assert "已保存个股原件 0份" in reused
    assert "发布缓存复用 2股" in reused and "原件缓存复用 0股" in reused


def test_cancellation_during_publication_cache_read_never_reports_industry_completed(fixture, monkeypatch):
    from dataclasses import replace

    source, _now, calls, _outcomes = fixture
    before = _fetch(source)
    cancelled = [False]
    latest = CurrentIndustryCache.latest
    messages = []

    def read(cache, at):
        result = latest(cache, at)
        cancelled[0] = True
        return result

    monkeypatch.setattr(CurrentIndustryCache, "latest", read)
    calls.clear()
    source = replace(source, report=messages.append, cancel_requested=lambda: cancelled[0])
    with pytest.raises(HttpCancelled):
        _fetch(source)
    assert calls == [] and not any(message.state == "完成" for message in messages)
    assert CurrentIndustryCache(source.path, sqlite_policy=source.sqlite_policy).load(_AT) == before


def test_waiting_feedback_names_current_stock_without_confirming_uncommitted_original(fixture, monkeypatch):
    from dataclasses import replace
    from io import StringIO

    from trader.download.entrypoints.sync_progress import SyncProgress
    from trader.download.infra.terminal_progress import TerminalDownloadProgress

    source, _now, _calls, _outcomes = fixture
    clock = [0.0]
    stream = StringIO()
    output = SyncProgress(TerminalDownloadProgress(stream), lambda: clock[0])
    source = replace(source, monotonic=lambda: clock[0], report=output)
    fetch = CninfoIndustryClient.fetch_changes

    def waiting(client, code, start, end, *, cancel_requested):
        clock[0] += 6
        output.heartbeat()
        line = " | ".join(part.strip() for part in stream.getvalue().splitlines()[-1].split(" | "))
        assert "等待 | 操作 获取个股行业 | 当前股票 600001" in line
        assert "已确认 0/1股" in line and "已保存个股原件" not in line
        assert "原件已保存" not in line
        assert "原件缓存未复用 无原件" in line
        assert "来源 巨潮" in line
        return fetch(client, code, start, end, cancel_requested=cancel_requested)

    monkeypatch.setattr(CninfoIndustryClient, "fetch_changes", waiting)
    _fetch(source)
    assert "接口获取（含解析）累计 6.000秒" in stream.getvalue().splitlines()[-1]
