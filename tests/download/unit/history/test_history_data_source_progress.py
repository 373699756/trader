"""Concrete history providers label observations without guessing an injected port."""

from io import StringIO

import pytest

from trader.download.entrypoints.sync_progress import SyncProgress
from trader.download.domain.history_sync import HistorySyncProgress
from trader.download.entrypoints.history_sync_progress import StderrHistorySyncProgress
from trader.download.infra.terminal_progress import TerminalDownloadProgress
from tests.helpers.download.history.history_sync_progress import _failed_status


@pytest.mark.parametrize("code", ("301548", "689009"))
def test_industry_candidate_cache_labels_stock_codes_without_a_date(code):
    stream = StringIO()
    reporter = StderrHistorySyncProgress(
        report=SyncProgress(TerminalDownloadProgress(stream), lambda: 0.0), monotonic=lambda: 0.0,
    )
    reporter.publish(HistorySyncProgress("supplier_industry", "completed", 4402, 4402, code,
                                         cache_hit=True, data_source="本地（巨潮原件）"))
    assert f"股票代码 {code}" in stream.getvalue()
    assert "日期" not in stream.getvalue() and "来源 本地（巨潮原件）" in stream.getvalue()


@pytest.mark.parametrize("state", ("started", "waiting", "completed", "failed"))
def test_industry_summary_and_failure_hide_internal_marker_instead_of_inventing_a_date(state):
    stream = StringIO()
    reporter = StderrHistorySyncProgress(
        report=SyncProgress(TerminalDownloadProgress(stream), lambda: 0.0), monotonic=lambda: 0.0,
    )
    source = "本地（中上协证据）" if state == "completed" else "中上协"
    reporter.publish(HistorySyncProgress("supplier_industry", state, 4402 if state == "completed" else 0,
                                         4402, "capco", data_source=source))
    if state == "failed":
        reporter.publish_result(_failed_status("history_fixture_failed"))
    output = stream.getvalue()
    assert f"来源 {source}" in output
    assert "capco" not in output and "日期" not in output


def test_calendar_keeps_its_real_date_range_label():
    stream = StringIO()
    reporter = StderrHistorySyncProgress(
        report=SyncProgress(TerminalDownloadProgress(stream), lambda: 0.0), monotonic=lambda: 0.0,
    )
    reporter.publish(HistorySyncProgress("supplier_calendar", "started", 0, 1, "2021-01-01:2026-09-11"))
    assert "日期范围 2021-01-01:2026-09-11" in stream.getvalue()


@pytest.mark.parametrize(
    "stage,source",
    (("supplier_login", "BaoStock"), ("supplier_calendar", "BaoStock"), ("supplier_universe", "BaoStock"),
     ("supplier_daily_raw", "BaoStock"), ("supplier_daily_qfq", "BaoStock"),
     ("supplier_industry", "本地（巨潮原件）"), ("supplier_industry", "中上协")),
)
def test_history_provider_source_is_visible_on_generic_stage(stage, source):
    stream = StringIO()
    output = SyncProgress(TerminalDownloadProgress(stream), lambda: 0.0)
    reporter = StderrHistorySyncProgress(report=output, monotonic=lambda: 0.0)

    reporter.publish(HistorySyncProgress(stage, "started", 0, 1, data_source=source))

    assert f"来源 {source}" in stream.getvalue()


@pytest.mark.parametrize("stage,source", (("tencent_history", "Tencent"), ("baostock_repair", "BaoStock")))
def test_history_provider_named_stage_does_not_repeat_source(stage, source):
    stream = StringIO()
    output = SyncProgress(TerminalDownloadProgress(stream), lambda: 0.0)
    reporter = StderrHistorySyncProgress(report=output, monotonic=lambda: 0.0)

    reporter.publish(HistorySyncProgress(stage, "started", 0, 1, data_source=source))

    assert source in stream.getvalue() and f"来源 {source}" not in stream.getvalue()


def test_unknown_history_calendar_port_does_not_guess_a_provider():
    stream = StringIO()
    reporter = StderrHistorySyncProgress(
        report=SyncProgress(TerminalDownloadProgress(stream), lambda: 0.0), monotonic=lambda: 0.0,
    )

    reporter.publish(HistorySyncProgress("supplier_calendar", "started", 0, 1))

    assert "来源 " not in stream.getvalue()
    assert "Tencent" not in stream.getvalue() and "BaoStock" not in stream.getvalue()


def test_history_data_source_does_not_change_price_route_contract():
    progress = HistorySyncProgress("supplier_calendar", "started", 0, 1, data_source="BaoStock")

    assert progress.supplier_source is None and progress.requested_sessions is None
    with pytest.raises(ValueError, match="history synchronization progress is invalid"):
        HistorySyncProgress("supplier_calendar", "started", 0, 1, data_source=" ")
