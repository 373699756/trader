"""Concrete history providers label observations without guessing an injected port."""

from io import StringIO

import pytest

from trader.download.application.sync_progress import SyncProgress
from trader.download.domain.history_sync import HistorySyncProgress
from trader.download.entrypoints.history_sync_progress import StderrHistorySyncProgress
from trader.download.infra.terminal_progress import TerminalDownloadProgress


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
