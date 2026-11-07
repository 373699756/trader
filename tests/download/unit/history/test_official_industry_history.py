from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from tests.helpers.shared.configuration import SqlitePolicyFixture
from trader.download.domain.baostock_daily import BaoStockSecurity
from trader.download.infra import official_industry_history as history_module
from trader.download.infra.industry_query_index import IndustrySourceDocument, SQLiteIndustryQueryIndex
from trader.download.infra.official_industry_history import OfficialIndustryHistory
from trader.download.infra.official_industry_pdf import OfficialIndustryRow, OfficialIndustrySnapshot
from trader.download.infra.official_industry_release import OfficialIndustryReleaseLink
from trader.infra.http_contracts import HttpCancelled, HttpResponse, HttpTimeout

_ROOT = "https://www.capco.org.cn/"
_BUNDLE = "https://www.capco.org.cn/assets/index-history.js"
_OLDER = "https://www.capco.org.cn/dataFile/zsxfb/hyfl/hyfljg/older.js"
_NEWER = "https://www.capco.org.cn/dataFile/zsxfb/hyfl/hyfljg/newer.js"
_FUTURE = "https://www.capco.org.cn/dataFile/zsxfb/hyfl/hyfljg/future.js"
_OLDER_PDF = "https://sp.capco.org.cn:82/file/202603/older.pdf"
_NEWER_PDF = "https://sp.capco.org.cn:82/file/202609/newer.pdf"
_OBSERVED = datetime(2026, 11, 7, 12, tzinfo=ZoneInfo("Asia/Shanghai"))
_UNIVERSE = (BaoStockSecurity("000001", "平安银行", "main", date(1991, 4, 3), None, "fixture"),)
_CALENDAR = (date(2026, 4, 2), date(2026, 9, 30), date(2026, 10, 8), date(2026, 11, 6))


def _link(url: str, period: date, disclosed: date) -> OfficialIndustryReleaseLink:
    return OfficialIndustryReleaseLink(
        announcement_url=url,
        disclosed_on=disclosed,
        title=f"{period.year}年{'上' if period.month == 6 else '下'}半年上市公司行业分类结果",
        period_end=period,
    )


def _announcement(link: OfficialIndustryReleaseLink, pdf_url: str) -> bytes:
    return ("export const allData = " + json.dumps({
        "date": link.disclosed_on.isoformat(),
        "source": "中国上市公司协会",
        "title": link.title,
        "text": f'<a href="{pdf_url}">{link.title}（按股票代码排序）.pdf</a>',
    }, ensure_ascii=False)).encode()


class _Transport:
    def __init__(self, responses):
        self.responses = responses
        self.requests = []
        self.on_request = lambda: None

    def get(self, url, *, timeout, deadline, max_response_bytes, cancel_requested):
        assert not cancel_requested()
        self.requests.append((url, timeout, deadline, max_response_bytes))
        self.on_request()
        response = self.responses[url]
        if isinstance(response, Exception):
            raise response
        return response


@pytest.fixture
def sources(monkeypatch):
    links = [
        _link(_OLDER, date(2025, 12, 31), date(2026, 3, 31)),
        _link(_NEWER, date(2026, 6, 30), date(2026, 9, 30)),
    ]
    calls = []
    monkeypatch.setattr(history_module, "ParseOfficialIndustryBundleUrl", lambda text: _BUNDLE)
    monkeypatch.setattr(history_module, "ParseOfficialIndustryReleaseLinks", lambda url, text: tuple(links))

    def parse(content, period_end, *, cancel_requested):
        assert not cancel_requested()
        calls.append(period_end)
        category, name = ("J66", "货币金融服务") if period_end.year == 2025 else ("J67", "资本市场服务")
        return OfficialIndustrySnapshot((
            OfficialIndustryRow("000001", "平安银行", category, name, 1, "J", "金融业"),
            OfficialIndustryRow("000002", "范围外证券", "C37", "运输设备制造业", 2, "C", "制造业", "CG", "运输设备"),
        ), 2)

    monkeypatch.setattr(history_module, "ParseOfficialIndustryPdf", parse)
    transport = _Transport({
        _ROOT: HttpResponse(200, b"root"),
        _BUNDLE: HttpResponse(200, b"bundle"),
        _OLDER: HttpResponse(200, _announcement(links[0], _OLDER_PDF)),
        _NEWER: HttpResponse(200, _announcement(links[1], _NEWER_PDF)),
        _OLDER_PDF: HttpResponse(200, b"%PDF-older"),
        _NEWER_PDF: HttpResponse(200, b"%PDF-newer"),
    })
    return links, calls, transport


def _history(tmp_path: Path, transport: _Transport, **kwargs) -> OfficialIndustryHistory:
    return OfficialIndustryHistory(
        index=SQLiteIndustryQueryIndex(tmp_path / "industry.sqlite3", "cninfo_industry_queries", sqlite_policy=SqlitePolicyFixture()),
        transport=transport,
        clock=kwargs.pop("clock", lambda: _OBSERVED),
        monotonic=kwargs.pop("monotonic", lambda: 100.0),
        **kwargs,
    )


def _evidence(history: OfficialIndustryHistory, observed_at: datetime = _OBSERVED):
    return history.index.read_evidence_window(_CALENDAR[0], _CALENDAR[-1], observed_at)


def _parsed_documents(history: OfficialIndustryHistory, observed_at: datetime = _OBSERVED):
    with sqlite3.connect(history.index.path.as_uri() + "?mode=ro", uri=True) as connection:
        keys = tuple(row[0] for row in connection.execute(
            "SELECT DISTINCT request_key FROM industry_source_document WHERE request_key LIKE 'capco:parsed:%'",
        ))
    return tuple(history.index.read_source_document(key, observed_at) for key in keys)


def test_official_chain_projects_from_publication_day_and_reuses_all_originals(tmp_path, sources):
    _links, calls, transport = sources
    history = _history(tmp_path, transport)
    cold = history.prepare(_UNIVERSE, _CALENDAR, _OBSERVED)
    assert cold.readiness.ready
    assert [(item.effective_from, item.effective_to, item.industry) for item in cold.intervals] == [
        (date(2026, 4, 1), date(2026, 10, 1), "capco:J66货币金融服务"),
        (date(2026, 10, 1), date(2026, 11, 8), "capco:J67资本市场服务"),
    ]
    records = _evidence(history)
    assert len(records) == 2 and {item.code for item in records} == {"000001"}
    assert records[0].announcement_sha256 == hashlib.sha256(transport.responses[_OLDER].content).hexdigest()
    assert records[0].document_sha256 == hashlib.sha256(b"%PDF-older").hexdigest()
    assert all(item.received_at == _OBSERVED and item.page == 1 for item in records)
    assert [request[0] for request in transport.requests] == [_ROOT, _BUNDLE, _OLDER, _OLDER_PDF, _NEWER, _NEWER_PDF]
    assert all(request[1] <= 45 and request[2] == 280 for request in transport.requests)
    assert transport.requests[-1][3] == 32 * 1024**2
    transport.requests.clear()
    warm = history.prepare(_UNIVERSE, _CALENDAR, _OBSERVED)
    assert warm == cold and transport.requests == []
    assert len(_evidence(history)) == 2
    assert calls == [date(2025, 12, 31), date(2026, 6, 30)]


def test_statistical_period_end_is_never_used_to_backfill_missing_history(tmp_path, sources):
    _links, _calls, transport = sources
    history = _history(tmp_path, transport)
    result = history.prepare(_UNIVERSE, (date(2026, 1, 5), *_CALENDAR), _OBSERVED)
    assert not result.readiness.ready and result.readiness.point_in_time_codes == 0
    assert min(item.effective_from for item in result.intervals) == date(2026, 4, 1)


def test_future_announcements_are_not_downloaded_or_used_for_projection(tmp_path, sources):
    links, _calls, transport = sources
    links.append(_link(_FUTURE, date(2026, 12, 31), date(2027, 3, 31)))
    result = _history(tmp_path, transport).prepare(_UNIVERSE, _CALENDAR, _OBSERVED)
    assert result.readiness.ready
    assert all(request[0] != _FUTURE for request in transport.requests)
    assert result.intervals[-1].effective_to == date(2026, 11, 8)


def test_failed_attachment_preserves_received_originals_and_resumes_only_missing_pdf(tmp_path, sources):
    _links, _calls, transport = sources
    transport.responses[_NEWER_PDF] = HttpTimeout("fixture_timeout")
    history = _history(tmp_path, transport)
    failed = history.prepare(_UNIVERSE, _CALENDAR, _OBSERVED)
    assert failed.readiness.failure_category == "official_industry_release_failed:2026-06-30:HttpTimeout"
    assert len(failed.intervals) == 1
    announcement_key = "capco:announcement:" + hashlib.sha256(_NEWER.encode()).hexdigest()
    assert history.index.read_source_document(announcement_key, _OBSERVED) is not None
    transport.responses[_NEWER_PDF] = HttpResponse(200, b"%PDF-newer")
    transport.requests.clear()
    recovered = history.prepare(_UNIVERSE, _CALENDAR, _OBSERVED)
    assert recovered.readiness.ready
    assert [item[0] for item in transport.requests] == [_NEWER_PDF]


def test_later_observation_extends_same_release_without_duplicate_evidence(tmp_path, sources):
    _links, _calls, transport = sources
    clock = [_OBSERVED]
    history = _history(tmp_path, transport, clock=lambda: clock[0])
    history.prepare(_UNIVERSE, _CALENDAR, _OBSERVED)
    clock[0] += timedelta(days=8)
    transport.requests.clear()
    result = history.prepare(_UNIVERSE, _CALENDAR, clock[0])
    assert result.readiness.ready
    assert result.intervals[-1].effective_to == date(2026, 11, 16)
    assert [item[0] for item in transport.requests] == [_ROOT, _BUNDLE, _OLDER, _OLDER_PDF, _NEWER, _NEWER_PDF]
    assert len(_evidence(history, clock[0])) == 2


def test_catalog_and_detail_disclosure_disagreement_does_not_publish_intervals(tmp_path, sources):
    links, _calls, transport = sources
    wrong = _link(_OLDER, date(2025, 12, 31), date(2026, 4, 1))
    transport.responses[_OLDER] = HttpResponse(200, _announcement(wrong, _OLDER_PDF))
    result = _history(tmp_path, transport).prepare(_UNIVERSE, _CALENDAR, _OBSERVED)
    assert result.readiness.failure_category == (
        "official_industry_release_failed:2025-12-31:official_industry_release_identity_conflict"
    )
    assert len(result.intervals) == 1 and result.intervals[0].effective_from == date(2026, 10, 1)
    assert all(item[0] != _OLDER_PDF for item in transport.requests)
    assert links[0].disclosed_on == date(2026, 3, 31)


def test_cancellation_after_request_stops_without_starting_next_request(tmp_path, sources):
    _links, _calls, transport = sources
    cancelled = [False]
    transport.on_request = lambda: cancelled.__setitem__(0, True)
    history = _history(tmp_path, transport, cancel_requested=lambda: cancelled[0])
    with pytest.raises(HttpCancelled, match="official_industry_cancelled"):
        history.prepare(_UNIVERSE, _CALENDAR, _OBSERVED)
    assert [item[0] for item in transport.requests] == [_ROOT]


def test_whole_preparation_deadline_stops_network_and_reports_incomplete(tmp_path, sources):
    _links, _calls, transport = sources
    monotonic = [100.0]
    transport.on_request = lambda: monotonic.__setitem__(0, 102.0)
    history = _history(tmp_path, transport, monotonic=lambda: monotonic[0], budget_seconds=1)
    result = history.prepare(_UNIVERSE, _CALENDAR, _OBSERVED)
    assert result.intervals == ()
    assert result.readiness.failure_category == "official_industry_budget_exhausted"
    assert transport.requests[0][1] == 1 and transport.requests[0][2] == 101
    assert len(transport.requests) == 1


def test_complete_parsed_snapshot_reprojects_scope_without_reparsing_pdf(tmp_path, sources):
    _links, calls, transport = sources
    history = _history(tmp_path, transport)
    history.prepare(_UNIVERSE, _CALENDAR, _OBSERVED)
    expanded = (*_UNIVERSE, BaoStockSecurity("000002", "范围外证券", "main", date(1991, 1, 29), None, "fixture"))
    transport.requests.clear()
    result = history.prepare(expanded, _CALENDAR, _OBSERVED)
    assert result.readiness.ready and result.readiness.point_in_time_codes == 2
    assert transport.requests == []
    assert calls == [date(2025, 12, 31), date(2026, 6, 30)]


def test_old_unsupported_pdf_does_not_block_later_legal_release(tmp_path, sources, monkeypatch):
    _links, _calls, transport = sources
    parse = history_module.ParseOfficialIndustryPdf

    def old_layout(content, period_end, *, cancel_requested):
        if period_end == date(2025, 12, 31):
            raise ValueError("official_industry_pdf_layout_unsupported")
        return parse(content, period_end, cancel_requested=cancel_requested)

    monkeypatch.setattr(history_module, "ParseOfficialIndustryPdf", old_layout)
    history = _history(tmp_path, transport)
    result = history.prepare(_UNIVERSE, _CALENDAR, _OBSERVED)
    assert result.readiness.failure_category == (
        "official_industry_release_failed:2025-12-31:official_industry_pdf_layout_unsupported"
    )
    assert not result.readiness.ready
    assert len(result.intervals) == 1 and result.intervals[0].effective_from == date(2026, 10, 1)
    assert _NEWER_PDF in [item[0] for item in transport.requests]
    assert len(_parsed_documents(history)) == 1


def test_catalog_date_change_refreshes_still_fresh_detail_once(tmp_path, sources):
    links, calls, transport = sources
    clock = [_OBSERVED]
    history = _history(tmp_path, transport, clock=lambda: clock[0])
    history.prepare(_UNIVERSE, _CALENDAR, _OBSERVED)
    links[1] = replace(links[1], disclosed_on=date(2026, 10, 1))
    transport.responses[_NEWER] = HttpResponse(200, _announcement(links[1], _NEWER_PDF))
    clock[0] += timedelta(days=1)
    transport.requests.clear()
    result = history.prepare(_UNIVERSE, _CALENDAR, clock[0])
    assert result.readiness.failure_category == (
        "official_industry_release_failed:2025-12-31:industry_evidence_interval_conflict;"
        "official_industry_release_failed:2026-06-30:official_industry_source_revision_unqualified"
    )
    assert [item[0] for item in transport.requests] == [_NEWER]
    assert calls == [date(2025, 12, 31), date(2026, 6, 30)]
    announcement_key = "capco:announcement:" + hashlib.sha256(_NEWER.encode()).hexdigest()
    assert history.index.read_source_document(announcement_key, clock[0]).content == transport.responses[_NEWER].content
    assert all(item.announcement_sha256 != hashlib.sha256(transport.responses[_NEWER].content).hexdigest()
               for item in _evidence(history, clock[0]))


def test_new_ipo_absent_from_complete_pdf_stays_missing_without_repeated_parse(tmp_path, sources):
    _links, calls, transport = sources
    history = _history(tmp_path, transport)
    history.prepare(_UNIVERSE, _CALENDAR, _OBSERVED)
    expanded = (*_UNIVERSE, BaoStockSecurity("000003", "新上市样本", "main", date(2026, 7, 1), None, "fixture"))
    transport.requests.clear()
    for _ in range(2):
        result = history.prepare(expanded, _CALENDAR, _OBSERVED)
        assert result.readiness.point_in_time_codes == 1 and not result.readiness.ready
    assert transport.requests == []
    assert calls == [date(2025, 12, 31), date(2026, 6, 30)]


def test_corrupt_parsed_cache_is_reported_and_preserved_without_reparse(tmp_path, sources):
    _links, calls, transport = sources
    clock = [_OBSERVED]
    history = _history(tmp_path, transport, clock=lambda: clock[0])
    history.prepare(_UNIVERSE, _CALENDAR, _OBSERVED)
    original = next(item for item in _parsed_documents(history) if json.loads(item.content)["period_end"] == "2025-12-31")
    clock[0] += timedelta(days=1)
    corrupted = IndustrySourceDocument(
        original.request_key, original.requested_from, original.requested_to, clock[0], b"{}",
    )
    history.index.save_source_documents((corrupted,))
    transport.requests.clear()
    result = history.prepare(_UNIVERSE, _CALENDAR, clock[0])
    assert result.readiness.failure_category == (
        "official_industry_release_failed:2025-12-31:official_industry_parsed_cache_invalid"
    )
    assert not result.readiness.ready and len(result.intervals) == 2
    assert history.index.read_source_document(original.request_key, clock[0]) == corrupted
    assert transport.requests == [] and calls == [date(2025, 12, 31), date(2026, 6, 30)]


def test_same_url_pdf_revision_without_new_disclosure_never_backdates_even_after_resume(tmp_path, sources):
    _links, calls, transport = sources
    clock = [_OBSERVED]
    history = _history(tmp_path, transport, clock=lambda: clock[0])
    history.prepare(_UNIVERSE, _CALENDAR, _OBSERVED)
    old_newer = next(item for item in _evidence(history) if item.source_url == _NEWER_PDF)
    clock[0] += timedelta(days=8)
    transport.responses[_NEWER_PDF] = HttpResponse(200, b"%PDF-revised-without-new-publication")
    first = history.prepare(_UNIVERSE, _CALENDAR, clock[0])
    reason = "official_industry_release_failed:2026-06-30:official_industry_source_revision_unqualified"
    assert first.readiness.failure_category == reason and not first.readiness.ready
    pdf_key = "capco:pdf:" + hashlib.sha256(_NEWER_PDF.encode()).hexdigest()
    assert history.index.read_source_document(pdf_key, clock[0]).content == transport.responses[_NEWER_PDF].content
    assert next(item for item in _evidence(history, clock[0]) if item.source_url == _NEWER_PDF) == old_newer
    transport.requests.clear()
    resumed = history.prepare(_UNIVERSE, _CALENDAR, clock[0])
    assert resumed.readiness.failure_category == reason and not resumed.readiness.ready
    assert transport.requests == [] and calls == [date(2025, 12, 31), date(2026, 6, 30)]


@pytest.mark.parametrize(
    ("case", "reason"),
    (
        ("duplicate_code", "official_industry_pdf_snapshot_invalid"),
        ("page_outside_snapshot", "official_industry_pdf_snapshot_invalid"),
        ("invalid_category", "official_industry_pdf_row_invalid"),
        ("boolean_page", "official_industry_parsed_row_invalid"),
        ("wrong_parser", "official_industry_parsed_cache_invalid"),
    ),
)
def test_parsed_cache_restoration_uses_shared_pdf_truth_constraints(case, reason):
    snapshot = OfficialIndustrySnapshot((OfficialIndustryRow("000001", "平安银行", "J66", "货币金融服务", 1),), 1)
    payload = json.loads(history_module._encode_snapshot(snapshot, "a" * 64, date(2026, 6, 30), "b" * 64))
    if case == "duplicate_code":
        payload["rows"].append(payload["rows"][0])
    elif case == "page_outside_snapshot":
        payload["rows"][0]["page"] = 2
    elif case == "invalid_category":
        payload["rows"][0]["native_category"] = "J6"
    elif case == "boolean_page":
        payload["rows"][0]["page"] = True
    else:
        payload["parser_implementation_sha256"] = "c" * 64
    with pytest.raises(ValueError, match=reason):
        history_module._decode_snapshot(
            json.dumps(payload).encode(), "a" * 64, date(2026, 6, 30), "b" * 64, lambda: None,
        )
