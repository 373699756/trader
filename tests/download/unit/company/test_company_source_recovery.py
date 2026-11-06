"""CNInfo bounds individual requests and retries without a task duration limit."""

import json
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from trader.download.domain.company_evidence import CompanySourceFailure, Disclosure, DisclosurePartition
from tests.helpers.shared.configuration import CninfoMarketDisclosureSourceFixture as CninfoMarketDisclosureSource
from trader.infra.http_contracts import HttpCancelled, HttpConnectionError, HttpError, HttpResponse, HttpTimeout

_DAY = date(2026, 10, 18)
_EMPTY = HttpResponse(200, json.dumps({"totalRecordNum": 0, "hasMore": False, "announcements": []}).encode())


class _Clock:
    def __init__(self):
        self.now = 0.0
        self.waits = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.waits.append(seconds)
        self.now += seconds


class _Transport:
    def __init__(self, clock, outcomes):
        self.clock = clock
        self.outcomes = list(outcomes)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((self.clock.now, kwargs))
        outcome, elapsed = self.outcomes.pop(0)
        self.clock.now += elapsed
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    get = post

    def execute(self, request):
        response = self.get(
            request.url, timeout=request.timeout_seconds, deadline=request.deadline,
            max_response_bytes=request.max_response_bytes, cancel_requested=request.cancel_requested,
            io_timeout_seconds=request.io_timeout_seconds,
        )
        if request.receive_headers is not None:
            request.receive_headers(response.status_code, response.header_items)
            request.receive_chunk(response.content)
        return response


@pytest.mark.parametrize(
    "transient",
    [
        HttpTimeout("fixture"),
        HttpConnectionError("fixture"),
        HttpError("http_transport_failed"),
        HttpError("http_compressed_body_invalid"),
        *[HttpResponse(code, b"") for code in (408, 500, 502, 503, 504)],
    ],
)
def test_transient_failure_recovers_same_page_with_independent_request_timeout(transient):
    clock = _Clock()
    transport = _Transport(clock, [(transient, 8), (_EMPTY, 0.1)])
    source = CninfoMarketDisclosureSource(transport, monotonic=clock, sleeper=clock.sleep)
    result = source.fetch(DisclosurePartition("sse", _DAY, _DAY, page=7))
    assert not result.has_more
    assert [stamp for stamp, _ in transport.calls] == [0, 10]
    first, second = [request for _, request in transport.calls]
    assert first["data"] == second["data"] and first["data"]["pageNum"] == "7"
    assert (first["deadline"], second["deadline"]) == (8, 18)
    assert first["timeout"] == second["timeout"] == 8
    assert sum(clock.waits) == 2


def test_pdf_uses_separate_total_deadline_and_bounded_io_and_retries(monkeypatch):
    clock = _Clock()
    transport = _Transport(clock, [(HttpTimeout("idle"), 8), (HttpResponse(200, b"%PDF-fixture"), 20)])
    source = CninfoMarketDisclosureSource(transport, monotonic=clock, sleeper=clock.sleep)
    monkeypatch.setattr(source, "_parse_document", lambda *_: "verified body " * 8)
    item = Disclosure(
        "cninfo", "slow", ("600001",), "风险公告", datetime(2026, 10, 18, tzinfo=ZoneInfo("Asia/Shanghai")),
        "https://static.cninfo.com.cn/finalpage/a.PDF", "a",
    )
    source.download_document(item)
    assert source.extract_document(item).text == "verified body " * 8
    first, second = [request for _, request in transport.calls]
    assert first["timeout"] == second["timeout"] == 120
    assert first["io_timeout_seconds"] == second["io_timeout_seconds"] == 8
    assert (first["deadline"], second["deadline"]) == (120, 130)
    assert len(transport.calls) == 2


def test_slow_pdf_reaches_existing_expanded_size_route(monkeypatch):
    clock = _Clock()
    transport = _Transport(clock, [
        (HttpResponse(200, b"%PDF-" + b" " * (17 * 1024 * 1024)), 29),
    ])
    source = CninfoMarketDisclosureSource(transport, monotonic=clock, sleeper=clock.sleep)

    def parse(_payload, limits):
        assert limits.max_bytes == 64 * 1024 * 1024
        return "verified body " * 8

    monkeypatch.setattr(source, "_parse_document", parse)
    item = Disclosure(
        "cninfo", "large", ("600001",), "风险公告", datetime(2026, 10, 18, tzinfo=ZoneInfo("Asia/Shanghai")),
        "https://static.cninfo.com.cn/finalpage/a.PDF", "a",
    )
    source.download_document(item)
    assert source.extract_document(item).text == "verified body " * 8
    assert [call["max_response_bytes"] for _, call in transport.calls] == [64 * 1024 * 1024]
    assert clock.now > 8 and len(transport.calls) == 1


@pytest.mark.parametrize("recover", [False, True])
def test_three_attempt_limit_and_increasing_backoff(recover):
    clock = _Clock()
    failure = HttpTimeout("fixture")
    transport = _Transport(clock, [(failure, 8), (failure, 8), (_EMPTY if recover else failure, 0.1)])
    source = CninfoMarketDisclosureSource(transport, monotonic=clock, sleeper=clock.sleep)
    if recover:
        assert not source.fetch(DisclosurePartition("sse", _DAY, _DAY)).has_more
    else:
        with pytest.raises(HttpTimeout):
            source.fetch(DisclosurePartition("sse", _DAY, _DAY))
    assert [stamp for stamp, _ in transport.calls] == [0, 10, 22]
    assert sum(clock.waits) == 6


@pytest.mark.parametrize("status,category", [(403, "access_denied"), (429, "rate_limited"), (404, "http_error")])
def test_rejection_is_not_retried(status, category):
    clock = _Clock()
    transport = _Transport(clock, [(HttpResponse(status, b""), 0.1)])
    source = CninfoMarketDisclosureSource(transport, monotonic=clock, sleeper=clock.sleep)
    with pytest.raises(CompanySourceFailure) as caught:
        source.fetch(DisclosurePartition("sse", _DAY, _DAY))
    assert caught.value.category == category
    assert len(transport.calls) == 1 and not clock.waits


def test_unsupported_encoding_is_not_retried():
    clock = _Clock()
    transport = _Transport(clock, [(HttpError("http_content_encoding_unsupported"), 0.1)])
    source = CninfoMarketDisclosureSource(transport, monotonic=clock, sleeper=clock.sleep)
    with pytest.raises(HttpError, match="http_content_encoding_unsupported"):
        source.fetch(DisclosurePartition("sse", _DAY, _DAY))
    assert len(transport.calls) == 1 and not clock.waits


def test_invalid_payload_is_not_retried():
    clock = _Clock()
    transport = _Transport(clock, [(HttpResponse(200, b"{}"), 0.1)])
    source = CninfoMarketDisclosureSource(transport, monotonic=clock, sleeper=clock.sleep)
    with pytest.raises(ValueError, match="cninfo_pagination_invalid"):
        source.fetch(DisclosurePartition("sse", _DAY, _DAY))
    assert len(transport.calls) == 1


@pytest.mark.parametrize("cancel_at,requests", [(0, 0), (8.25, 1)])
def test_cancellation_before_request_or_during_backoff_stops_new_attempts(cancel_at, requests):
    clock = _Clock()
    transport = _Transport(clock, [(HttpTimeout("fixture"), 8)])

    def cancel():
        return clock.now >= cancel_at

    source = CninfoMarketDisclosureSource(transport, monotonic=clock, sleeper=clock.sleep, cancel_requested=cancel)
    with pytest.raises(HttpCancelled):
        source.fetch(DisclosurePartition("sse", _DAY, _DAY))
    assert len(transport.calls) == requests and clock.now == cancel_at
    if requests:
        assert transport.calls[0][1]["cancel_requested"] is cancel


def test_document_download_retries_before_parsing_once(monkeypatch):
    clock = _Clock()
    transport = _Transport(clock, [(HttpTimeout("fixture"), 8), (HttpResponse(200, b"%PDF-fixture"), 0.1)])
    parsed = []

    def parse(self, payload, limits=None):
        parsed.append(payload)
        assert self._document_parse_timeout == 4.0
        return "verified body " * 8

    monkeypatch.setattr(CninfoMarketDisclosureSource, "_parse_document", parse)
    source = CninfoMarketDisclosureSource(
        transport,
        monotonic=clock,
        sleeper=clock.sleep,
        document_parse_timeout_seconds=4.0,
        request_interval_seconds=2.0,
    )
    item = Disclosure(
        "cninfo",
        "fixture",
        ("600001",),
        "fixture",
        datetime(2026, 10, 18, tzinfo=ZoneInfo("Asia/Shanghai")),
        "https://static.cninfo.com.cn/finalpage/a.PDF",
        "a",
    )
    source.download_document(item)
    assert source.extract_document(item).text == "verified body " * 8
    assert len(transport.calls) == 2 and len(parsed) == 1 and parsed[0].read_bytes() == b"%PDF-fixture"
    assert transport.calls[-1][1]["max_response_bytes"] == 64 * 1024 * 1024
    assert clock.now >= 2.0
