import pytest

from trader.download.application.progress_message import ProgressMessage
from trader.download.entrypoints.sync_progress import SyncProgress
from trader.download.domain.history_sync import HistorySyncProgress
from trader.download.entrypoints.history_sync_progress import StderrHistorySyncProgress


def test_repair_prints_one_result_per_stock_without_daily_call_heartbeats() -> None:
    now = [0.0]
    messages: list[ProgressMessage] = []
    output = SyncProgress(messages.append, lambda: now[0])
    progress = StderrHistorySyncProgress(monotonic=lambda: now[0], report=output)
    progress.publish(HistorySyncProgress("tencent_history", "completed", 4402, 4402))
    progress.publish(HistorySyncProgress("baostock_repair", "started", 0, 25))
    messages.clear()
    progress.publish(HistorySyncProgress("supplier_daily_raw", "started", 0, 1, "sz.000708"))
    progress.publish(HistorySyncProgress("supplier_daily_qfq", "started", 0, 1, "sz.000708"))
    assert messages == []
    now[0] = 5.0
    output.heartbeat()
    keys = {message.task.key for message in messages if message.task is not None}
    assert keys == set()
    progress.publish(HistorySyncProgress("baostock_repair", "failed", 1, 25, "000708", failure_reason="timeout"))
    fields = " | ".join(field.text for field in messages[-1].fields)
    assert "000708" in fields and "25" in fields and "timeout" in fields
    assert "4402" not in fields
    messages.clear()
    progress.publish(HistorySyncProgress("baostock_repair", "obtained", 2, 25, "000709"))
    progress.publish(HistorySyncProgress("baostock_repair", "obtained", 3, 25, "000710"))
    assert len(messages) == 2
    assert "000709" in " ".join(field.text for field in messages[0].fields)
    assert "000710" in " ".join(field.text for field in messages[1].fields)
    assert all("获取成功，待落库" in " ".join(field.text for field in message.fields) for message in messages)
    messages.clear()
    now[0] = 10.0
    output.heartbeat()
    assert messages == []


@pytest.mark.parametrize("stage", ("supplier_daily_raw", "supplier_daily_qfq"))
def test_repair_retry_shows_backoff_and_attempt_without_repeating_daily_heartbeats(stage):
    now = [0.0]
    messages: list[ProgressMessage] = []
    output = SyncProgress(messages.append, lambda: now[0])
    progress = StderrHistorySyncProgress(monotonic=lambda: now[0], report=output)
    progress.publish(HistorySyncProgress("tencent_history", "completed", 4402, 4402))
    progress.publish(HistorySyncProgress("baostock_repair", "started", 0, 25))
    messages.clear()

    for attempt, delay in ((2, 15), (3, 30), (4, 60)):
        progress.publish(HistorySyncProgress(
            stage, "retrying", 0, 1, "300390", attempt=attempt, max_attempts=4,
            retry_delay_seconds=delay, data_source="BaoStock",
        ))
        fields = " | ".join(field.text for field in messages[-1].fields)
        assert f"尝试 {attempt}/4" in fields
        wait = f"00:00:{delay:02d}" if delay < 60 else "00:01:00"
        assert f"等待 {wait} 后重试" in fields
        assert "300390" in fields and "4402" not in fields
        assert "总进度 0/ 25" in " ".join(fields.split())
        assert messages[-1].state == "重试" and not messages[-1].heartbeat
        messages.clear()
        now[0] += 5.0
        progress.publish(HistorySyncProgress(stage, "waiting", 0, 1, "300390"))
        output.heartbeat()
        assert messages == []

    progress.publish(HistorySyncProgress(
        stage, "failed", 0, 1, "300390", attempt=4, max_attempts=4, failure_reason="supplier_price_total_timeout",
    ))
    progress.publish(HistorySyncProgress("baostock_repair", "failed", 1, 25, "300390", failure_reason="supplier_price_total_timeout"))
    assert len(messages) == 1
    assert "supplier_price_total_timeout" in " ".join(field.text for field in messages[0].fields)
