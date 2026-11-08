"""Offline ST request budgets, cancellation, and durable progress boundaries."""

from concurrent.futures import Future
from contextlib import nullcontext
from dataclasses import replace
from datetime import date, timedelta
from threading import Event
from typing import cast

import pytest

from tests.helpers.download.history.history_supplier_router import _current_universe
from trader.download.domain.history_reference import HistoryStEvidence
from trader.download.infra.history_st_source import HistoryStNameSource
from trader.download.infra.reference_index import SQLiteReferenceIndex
from trader.infra.http_contracts import (
    HttpCancelled, HttpConnectionError, HttpError, HttpResponse, HttpStatusError, HttpTimeout, HttpTransport,
)

_DAY = date(2026, 11, 8)
_CLEAR = HttpResponse(200, "<td>证券简称更名历史：</td><td>正常公司</td>".encode("gb18030"))


class _InlineExecutor:
    def submit(self, function, /, *args, **kwargs):
        future = Future()
        try:
            future.set_result(function(*args, **kwargs))
        except BaseException as exc:
            future.set_exception(exc)
        return future


class _Clock:
    def __init__(self):
        self.value = 0.0
        self.waits = []
        self.on_sleep = None

    def now(self):
        return self.value

    def sleep(self, duration):
        self.waits.append(duration)
        self.value += duration
        if self.on_sleep is not None:
            self.on_sleep()


class _Index:
    def __init__(self, evidence=()):
        self.evidence = evidence
        self.reads = 0
        self.writes = []
        self.on_write = None

    def read_st(self):
        self.reads += 1
        return self.evidence

    def merge_st(self, incoming, observed):
        if self.on_write is not None:
            self.on_write()
        self.writes.append(incoming)
        self.evidence = incoming
        return incoming


class _Transport:
    def __init__(self, *steps):
        self.steps = list(steps)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        step = self.steps.pop(0) if self.steps else _CLEAR
        if isinstance(step, BaseException):
            raise step
        return step(kwargs) if callable(step) else step


def _source(index, transport, *, clock=None, cancelled=None, executor=None):
    clock = clock or _Clock()
    cancelled = cancelled or Event()
    reports = []
    source = HistoryStNameSource(
        cast(SQLiteReferenceIndex, index), lambda: nullcontext(cast(HttpTransport, transport)),
        executor or _InlineExecutor(), cancelled.is_set, report=reports.append,
        monotonic=clock.now, sleeper=clock.sleep,
    )
    return source, reports


@pytest.mark.parametrize("failure", (
    HttpTimeout("private payload"), HttpConnectionError("private payload"),
    *(HttpStatusError(status) for status in (408, 429, 500, 502, 503, 504)),
))
def test_transient_st_failure_retries_then_accepts_real_response(failure):
    index, clock, transport = _Index(), _Clock(), _Transport(failure, _CLEAR)
    source, reports = _source(index, transport, clock=clock)
    result = source.fetch(_current_universe("600001"), _DAY)
    assert result[0].status == "clear" and len(transport.calls) == 2
    assert {call[1]["deadline"] for call in transport.calls} == {30.0}
    assert all(call[1]["timeout"] <= 15 for call in transport.calls)
    assert clock.waits and max(clock.waits) <= 0.05
    assert not any(report.failures for report in reports)
    assert reports[-1].processed == reports[-1].persisted == 1 and reports[-1].summary.clear == 1


def test_st_retries_exhaust_exact_attempt_count_without_leaking_error_payload():
    transport = _Transport(*(HttpConnectionError("private payload") for _ in range(2)))
    source, reports = _source(_Index(), transport)
    assert source.fetch(_current_universe("600001"), _DAY)[0].status == "unknown"
    failures = [failure for report in reports for failure in report.failures]
    assert len(transport.calls) == 2 and len(failures) == 1
    failure = failures[0]
    assert failure.attempts == failure.maximum_attempts == 2
    assert failure.reason == "history_st_connection_failed" and "HttpConnectionError" in failure.diagnostic
    assert "private payload" not in failure.diagnostic and failure.elapsed_seconds == pytest.approx(0.25)
    assert reports[-1].summary.unknown == 1


@pytest.mark.parametrize("names", ("", " \t\r\n ", "<br/><span></span>", "&nbsp;&#160;", "\u3000\u00a0", "-", "--", "暂无", "暂无数据"))
def test_missing_history_is_unknown_without_retry_and_can_be_checked_again(names):
    empty_history = HttpResponse(200, f"<td>证券简称更名历史：</td><td>{names}</td>".encode("gb18030"))
    index, clock, transport = _Index(), _Clock(), _Transport(empty_history, _CLEAR)
    source, reports = _source(index, transport, clock=clock)
    universe = _current_universe("600001")

    result = source.fetch(universe, _DAY)

    assert result == (HistoryStEvidence("600001", _DAY, "unknown"),)
    assert index.evidence == result and len(transport.calls) == 1 and clock.waits == []
    failure = next(failure for report in reports for failure in report.failures)
    assert failure.reason == "history_st_name_history_empty" and failure.attempts == 1
    assert reports[-1].summary.unknown == 1 and reports[-1].summary.clear == 0

    next_day = _DAY + timedelta(days=1)
    recovered = source.fetch(universe, next_day)

    assert recovered == (HistoryStEvidence("600001", next_day, "clear"),)
    assert index.evidence == recovered and len(transport.calls) == 2
    assert reports[-1].summary.clear == 1 and reports[-1].summary.unknown == 0


def test_current_st_name_remains_excluded_without_requesting_missing_history():
    index, transport = _Index(), _Transport()
    source, reports = _source(index, transport)
    universe = (replace(_current_universe("600001")[0], name="*ST测试"),)

    result = source.fetch(universe, _DAY)

    assert result == (HistoryStEvidence("600001", _DAY, "ever_st"),)
    assert index.evidence == result and transport.calls == []
    assert reports[-1].summary.ever_st == 1


def test_st_session_acquisition_failure_does_not_invent_http_attempts():
    reports = []

    def unavailable():
        raise HttpConnectionError("private payload")

    source = HistoryStNameSource(cast(SQLiteReferenceIndex, _Index()), unavailable, _InlineExecutor(),
        lambda: False, report=reports.append)
    assert source.fetch(_current_universe("600001"), _DAY)[0].status == "unknown"
    failure = next(failure for report in reports for failure in report.failures)
    assert failure.attempts == 0 and failure.maximum_attempts == 2


def test_st_timeouts_share_thirty_second_budget_without_third_request():
    clock = _Clock()

    def timeout(kwargs):
        clock.value += kwargs["timeout"]
        raise HttpTimeout("private payload")

    transport = _Transport(timeout, timeout)
    source, reports = _source(_Index(), transport, clock=clock)
    assert source.fetch(_current_universe("600001"), _DAY)[0].status == "unknown"
    failure = next(failure for report in reports for failure in report.failures)
    assert len(transport.calls) == failure.attempts == failure.maximum_attempts == 2
    assert transport.calls[1][1]["timeout"] == pytest.approx(14.75)
    assert failure.elapsed_seconds == pytest.approx(30.0) and failure.timeout_seconds == pytest.approx(14.75)
    assert failure.reason == "history_st_timeout"


@pytest.mark.parametrize(("response", "reason"), (
    (HttpResponse(200, b"maintenance"), "history_st_name_history_missing"),
    (HttpResponse(200, b"\xff"), "history_st_decode_failed"),
    (HttpResponse(403, b""), "http_403"),
    (HttpResponse(301, b""), "http_301"),
    (HttpError("http_content_encoding_unsupported"), "http_content_encoding_unsupported"),
))
def test_st_parse_errors_and_permanent_http_failures_are_not_retried(response, reason):
    clock, transport = _Clock(), _Transport(response)
    source, reports = _source(_Index(), transport, clock=clock)
    assert source.fetch(_current_universe("600001"), _DAY)[0].status == "unknown"
    failure = next(failure for report in reports for failure in report.failures)
    assert len(transport.calls) == failure.attempts == 1 and clock.waits == []
    assert failure.reason == reason and failure.code == "600001"


@pytest.mark.parametrize("phase", ("response", "backoff", "accepted_future", "http_cancelled"))
def test_cancelled_st_check_never_persists_or_accepts_unknown(phase):
    cancelled, clock, index = Event(), _Clock(), _Index()

    def response(kwargs):
        cancelled.set()
        return _CLEAR

    class CompletedThenCancelled(_InlineExecutor):
        def submit(self, *args, **kwargs):
            future = super().submit(*args, **kwargs)
            cancelled.set()
            return future

    transport = _Transport(response if phase == "response" else
        HttpConnectionError("private payload") if phase == "backoff" else
        HttpCancelled("http_cancelled") if phase == "http_cancelled" else _CLEAR)
    if phase == "backoff":
        clock.on_sleep = cancelled.set
    source, reports = _source(index, transport, clock=clock, cancelled=cancelled,
        executor=CompletedThenCancelled() if phase == "accepted_future" else None)
    with pytest.raises(HttpCancelled if phase == "http_cancelled" else RuntimeError):
        source.fetch(_current_universe("600001"), _DAY)
    assert len(transport.calls) == 1 and index.writes == []
    assert all(report.processed == report.persisted == 0 and not report.failures and report.summary is None for report in reports)


def test_st_checkpoint_progress_only_confirms_rows_after_original_writes_return():
    index, transport = _Index(), _Transport()
    source, reports = _source(index, transport)
    before_writes = []

    def writing():
        progress = reports[-1]
        before_writes.append((progress.processed, progress.persisted))

    index.on_write = writing
    universe = _current_universe(*(f"600{number:03d}" for number in range(130)))
    assert len(source.fetch(universe, _DAY)) == 130
    assert before_writes == [(64, 0), (128, 64), (130, 128), (130, 130)]
    assert reports[-1].processed == reports[-1].persisted == 130 and reports[-1].summary.clear == 130


def test_st_failed_checkpoint_does_not_claim_persistence_or_final_completion():
    index, transport = _Index(), _Transport()
    source, reports = _source(index, transport)

    def fail():
        raise OSError("write failed")

    index.on_write = fail
    with pytest.raises(OSError, match="write failed"):
        source.fetch(_current_universe(*(f"600{number:03d}" for number in range(64))), _DAY)
    assert reports[-1].processed == 64 and reports[-1].persisted == 0
    assert all(report.summary is None for report in reports) and index.writes == []


def test_cancelled_st_write_keeps_its_receipt_without_claiming_final_completion():
    cancelled, index = Event(), _Index()
    source, reports = _source(index, _Transport(), cancelled=cancelled)
    index.on_write = cancelled.set
    with pytest.raises(RuntimeError, match="cancelled"):
        source.fetch(_current_universe("600001"), _DAY)
    assert len(index.writes) == 1 and index.evidence[0].status == "clear"
    assert all(report.persisted == 0 and report.summary is None for report in reports)


def test_st_cache_reuses_only_permanent_or_same_day_clear_and_rechecks_unknown():
    previous = (
        HistoryStEvidence("600001", _DAY, "clear"),
        HistoryStEvidence("600002", _DAY - timedelta(days=30), "ever_st"),
        HistoryStEvidence("600003", _DAY, "unknown"),
        HistoryStEvidence("600004", _DAY - timedelta(days=1), "clear"),
    )
    index, transport = _Index(previous), _Transport()
    source, reports = _source(index, transport)
    universe = _current_universe(*(item.code for item in previous))
    assert len(source.fetch(universe, _DAY)) == 4
    assert len(transport.calls) == 2 and all(any(code in call[0] for code in ("600003", "600004")) for call in transport.calls)
    assert reports[0].processed == reports[0].persisted == 2
    transport.calls.clear()
    reports.clear()
    assert len(source.fetch(universe, _DAY)) == 4 and transport.calls == []
    assert len(reports) == 1 and reports[0].cache_only and reports[0].summary.clear == 3
