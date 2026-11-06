from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from urllib.parse import parse_qs, urlsplit

import pytest

from trader.download.infra.cninfo_industry import (
    CninfoIndustryClient,
    CninfoIndustryError,
    ParseCninfoIndustryChanges,
    ParseCninfoIndustryTaxonomy,
    ResolveCninfoIndustryCategory,
)
from trader.infra.http_contracts import HttpCancelled, HttpRequest, HttpResponse, HttpStatusError, HttpTimeout
from trader.infra.http_transport import RequestsHttpTransport

_STANDARD_NAME = "中国上市公司协会上市公司行业分类标准"


def _taxonomy():
    return [
        {"SORTCODE": code, "PARENTCODE": parent, "SORTNAME": name, "F001V": None,
         "F002D": None, "F003V": "008001", "F004V": _STANDARD_NAME}
        for code, parent, name in (
            ("0", "008", "中国上市公司协会上市公司行业分类"),
            ("A", "0", "农、林、牧、渔业"),
            ("A01", "A", "农业"),
            ("A011", "A01", "谷物种植"),
        )
    ]


def _change(**fields):
    return {
        "SECCODE": "001222", "SECNAME": "源飞宠物", "ORGNAME": "温州源飞宠物玩具制品股份有限公司",
        "VARYDATE": "2024-09-30", "F001V": "008001", "F002V": _STANDARD_NAME,
        "F003V": "C243", "F004V": "制造业", "F005V": "造纸、印刷、文教",
        "F006V": "文教、工美、体育和娱乐用品制造业", "F007V": "工艺美术及礼仪用品制造", "F008C": "1",
        **fields,
    }


def _response(records, **fields):
    return HttpResponse(200, json.dumps({"resultcode": 200, "count": len(records), "records": records, **fields}, ensure_ascii=False).encode())


def _client(response, *, monotonic_clock=lambda: 100.0, headers=lambda: (("Accept-Enckey", "fixture-only"),)):
    calls = []

    def execute(request):
        calls.append(request)
        return response(request) if callable(response) else response

    transport = RequestsHttpTransport(execute_request=execute, monotonic=monotonic_clock)
    client = CninfoIndustryClient(transport, request_headers=headers, timeout_seconds=10, monotonic_clock=monotonic_clock)
    return client, transport, calls


def test_taxonomy_retains_native_hierarchy_and_exact_raw_digest():
    response = _response(_taxonomy())
    client, transport, calls = _client(response)
    with transport:
        result = client.fetch_taxonomy(deadline=104)
    assert tuple(row.code for row in result.rows) == ("0", "A", "A01", "A011")
    assert result.rows[-1].parent_code == "A01"
    assert result.rows[-1].name == "谷物种植"
    assert result.rows[-1].standard_name == _STANDARD_NAME
    assert result.rows[-1].ended_on is None
    assert result.response_content == response.content
    assert result.response_sha256 == hashlib.sha256(response.content).hexdigest()
    assert len(calls) == 1
    assert calls[0].method == "GET"
    assert parse_qs(urlsplit(calls[0].url).query) == {"indtype": ["008001"], "format": ["json"]}
    assert calls[0].timeout_seconds == 4
    assert calls[0].deadline == 104
    assert calls[0].max_response_bytes == 2 * 1024 * 1024
    assert ("Accept-Enckey", "fixture-only") in calls[0].headers


@pytest.mark.parametrize("latest", (0, 1, "0", "1"))
def test_changes_preserve_native_standard_date_and_latest_marker(latest):
    response = _response([_change(F008C=latest)])
    client, transport, calls = _client(response)
    with transport:
        result = client.fetch_changes("001222", date(2024, 1, 1), date(2026, 11, 6))
    row, = result.rows
    assert row.code == "001222"
    assert row.changed_on == date(2024, 9, 30)
    assert row.industry_code == "C243"
    assert row.category_name == "文教、工美、体育和娱乐用品制造业"
    assert row.standard_code == "008001"
    assert row.standard_name == _STANDARD_NAME
    assert row.latest_marker == latest and type(row.latest_marker) is type(latest)
    assert row.is_latest is (latest in (1, "1"))
    assert result.response_content == response.content
    assert result.response_sha256 == hashlib.sha256(response.content).hexdigest()
    assert len(calls) == 1 and calls[0].method == "POST"
    assert parse_qs(urlsplit(calls[0].url).query) == {
        "scode": ["001222"], "sdate": ["2024-01-01"], "edate": ["2026-11-06"],
    }


def test_successful_empty_changes_are_explicit_empty_evidence():
    client, transport, calls = _client(_response([]))
    with transport:
        assert client.fetch_changes("001222", date(2024, 1, 1), date(2026, 11, 6)).rows == ()
    assert len(calls) == 1


@pytest.mark.parametrize("overrides,reason", [
    ({"SECCODE": "002594"}, "response_identity_invalid"),
    ({"VARYDATE": "2023-12-31"}, "response_identity_invalid"),
    ({"VARYDATE": "2026-11-07"}, "response_identity_invalid"),
    ({"VARYDATE": "2024-09-30 00:00:00"}, "date_invalid"),
    ({"VARYDATE": "2024-02-30"}, "date_invalid"),
    ({"F008C": None}, "latest_marker_invalid"),
    ({"F008C": True}, "latest_marker_invalid"),
    ({"F008C": "Y"}, "latest_marker_invalid"),
    ({"F003V": ""}, "field_invalid"),
])
def test_wrong_standard_identity_dates_or_latest_marker_reject_entire_query(overrides, reason):
    client, transport, calls = _client(_response([_change(**overrides)]))
    with transport, pytest.raises(CninfoIndustryError, match=reason):
        client.fetch_changes("001222", date(2024, 1, 1), date(2026, 11, 6))
    assert len(calls) == 1


@pytest.mark.parametrize("records,reason", [
    ([_change(), _change()], "change_conflict"),
    ([_change(), _change(VARYDATE="2024-06-30")], "latest_conflict"),
])
def test_conflicting_history_is_not_compressed_into_a_usable_interval(records, reason):
    client, transport, calls = _client(_response(records))
    with transport, pytest.raises(CninfoIndustryError, match=reason):
        client.fetch_changes("001222", date(2024, 1, 1), date(2026, 11, 6))
    assert len(calls) == 1


def test_changes_are_chronological_and_do_not_refresh_their_change_date():
    client, transport, calls = _client(_response([_change(), _change(VARYDATE="2024-06-30", F008C="0")]))
    with transport:
        result = client.fetch_changes("001222", date(2024, 1, 1), date(2026, 11, 6))
    assert tuple(row.changed_on for row in result.rows) == (date(2024, 6, 30), date(2024, 9, 30))
    assert len(calls) == 1


@pytest.mark.parametrize("mutation,reason", [
    (lambda rows: [], "taxonomy_incomplete"),
    (lambda rows: rows + [rows[-1]], "taxonomy_incomplete"),
    (lambda rows: [rows[0], *[{**row, "PARENTCODE": "missing"} for row in rows[1:]]], "taxonomy_parent_missing"),
    (lambda rows: [{**row, "F004V": "证监会行业分类标准"} for row in rows], "standard_unexpected"),
])
def test_taxonomy_must_be_complete_and_current_standard(mutation, reason):
    client, transport, calls = _client(_response(mutation(_taxonomy())))
    with transport, pytest.raises(CninfoIndustryError, match=reason):
        client.fetch_taxonomy()
    assert len(calls) == 1


@pytest.mark.parametrize("response,reason", [
    (_response([], resultcode=402), "query_rejected"),
    (_response([], resultcode="200"), "query_rejected"),
    (_response([], count=1), "records_incomplete"),
    (_response([], count=True), "records_incomplete"),
    (_response(["invalid"]), "record_invalid"),
    (HttpResponse(200, b"not-json"), "json_invalid"),
    (HttpResponse(200, b"\xff"), "json_invalid"),
    (HttpResponse(200, b""), "response_size_invalid"),
])
def test_http_success_does_not_skip_business_or_structural_checks(response, reason):
    client, transport, calls = _client(response)
    with transport, pytest.raises(CninfoIndustryError, match=reason):
        client.fetch_taxonomy()
    assert len(calls) == 1


def test_http_failure_is_not_retried_or_reclassified_as_empty():
    client, transport, calls = _client(HttpResponse(503, b"unavailable"))
    with transport, pytest.raises(HttpStatusError) as raised:
        client.fetch_taxonomy()
    assert raised.value.status_code == 503
    assert len(calls) == 1


@pytest.mark.parametrize("code,start,end", [
    ("", date(2024, 1, 1), date(2026, 11, 6)),
    ("001222,002594", date(2024, 1, 1), date(2026, 11, 6)),
    ("００１２２２", date(2024, 1, 1), date(2026, 11, 6)),
    ("001222", date(2026, 11, 7), date(2026, 11, 6)),
    ("001222", datetime(2024, 1, 1), date(2026, 11, 6)),
])
def test_invalid_query_never_authenticates_or_sends_a_request(code, start, end):
    client, transport, calls = _client(_response([]), headers=lambda: pytest.fail("must validate before authorization"))
    with transport, pytest.raises(ValueError, match="query_invalid"):
        client.fetch_changes(code, start, end)
    assert calls == []


@pytest.mark.parametrize("deadline", (99, 100))
def test_expired_deadline_stops_before_authorization(deadline):
    client, transport, calls = _client(_response([]), headers=lambda: pytest.fail("deadline before authorization"))
    with transport, pytest.raises(HttpTimeout):
        client.fetch_taxonomy(deadline=deadline)
    assert calls == []


def test_cancellation_stops_before_authorization():
    client, transport, calls = _client(_response([]), headers=lambda: pytest.fail("cancel before authorization"))
    with transport, pytest.raises(HttpCancelled):
        client.fetch_taxonomy(cancel_requested=lambda: True)
    assert calls == []


def test_cancellation_after_response_preserves_the_transport_lifecycle():
    cancelled = False

    def response(request: HttpRequest):
        nonlocal cancelled
        cancelled = True
        return _response([])

    client, transport, calls = _client(response)
    with transport, pytest.raises(HttpCancelled):
        client.fetch_taxonomy(cancel_requested=lambda: cancelled)
    assert len(calls) == 1


def test_authorization_time_counts_against_the_same_deadline():
    current = 100.0

    def headers():
        nonlocal current
        current = 111.0
        return (("Accept-Enckey", "fixture-only"),)

    client, transport, calls = _client(_response([]), monotonic_clock=lambda: current, headers=headers)
    with transport, pytest.raises(HttpTimeout):
        client.fetch_taxonomy()
    assert calls == []


def test_missing_authorization_is_a_configuration_error_without_network():
    client, transport, calls = _client(_response([]), headers=lambda: ())
    with transport, pytest.raises(ValueError, match="authorization_missing"):
        client.fetch_taxonomy()
    assert calls == []


def test_old_and_unknown_standards_remain_native_and_never_resolve_as_current():
    content = _response([
        _change(F002V="证监会行业分类2012"),
        _change(F001V="008003", F002V="申银万国行业分类标准"),
        _change(),
    ]).content
    parsed = ParseCninfoIndustryChanges(content, "001222", date(2024, 1, 1), date(2026, 11, 6))
    assert len(parsed.rows) == 3
    assert sum(row.is_current_standard for row in parsed.rows) == 1
    taxonomy = ParseCninfoIndustryTaxonomy(_response(_taxonomy()).content)
    for row in parsed.rows:
        if not row.is_current_standard:
            assert ResolveCninfoIndustryCategory(taxonomy, row) is None
            assert row.standard_name in {"证监会行业分类2012", "申银万国行业分类标准"}
    assert parsed.response_content == content


def test_cache_parser_revalidates_envelope_and_retains_bytes_without_network():
    content = _response(_taxonomy()).content
    taxonomy = ParseCninfoIndustryTaxonomy(content)
    assert taxonomy.response_sha256 == hashlib.sha256(content).hexdigest()
    assert taxonomy.response_content == content
    with pytest.raises(CninfoIndustryError, match="records_incomplete"):
        ParseCninfoIndustryTaxonomy(_response(_taxonomy(), count=20).content)


def test_native_leaf_resolves_only_through_declared_taxonomy_parent_chain():
    taxonomy = ParseCninfoIndustryTaxonomy(_response(_taxonomy()).content)
    change, = ParseCninfoIndustryChanges(
        _response([_change(F003V="A011")]).content, "001222", date(2024, 1, 1), date(2026, 11, 6)
    ).rows
    category = ResolveCninfoIndustryCategory(taxonomy, change)
    assert category is not None
    assert category.code == "A01" and category.name == "农业"
    missing_change, = ParseCninfoIndustryChanges(
        _response([_change(F003V="A012")]).content, "001222", date(2024, 1, 1), date(2026, 11, 6)
    ).rows
    with pytest.raises(CninfoIndustryError, match="category_missing"):
        ResolveCninfoIndustryCategory(taxonomy, missing_change)


def test_taxonomy_cycles_are_explicitly_rejected():
    rows = _taxonomy()
    rows[1]["PARENTCODE"] = "A011"
    with pytest.raises(CninfoIndustryError, match="taxonomy_cycle"):
        ParseCninfoIndustryTaxonomy(_response(rows).content)


def test_cache_parser_cancellation_cannot_be_turned_into_empty_evidence():
    with pytest.raises(HttpCancelled):
        ParseCninfoIndustryTaxonomy(_response(_taxonomy()).content, cancel_requested=lambda: True)
    with pytest.raises(HttpCancelled):
        ParseCninfoIndustryChanges(
            _response([]).content, "001222", date(2024, 1, 1), date(2026, 11, 6), cancel_requested=lambda: True
        )
