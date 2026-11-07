from trader.download.application.progress_message import ProgressMessage
from trader.download.application.sync_progress import SyncProgress
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
    assert keys == {"baostock_repair"}
    progress.publish(HistorySyncProgress("baostock_repair", "failed", 1, 25, "000708", failure_reason="timeout"))
    fields = " | ".join(field.text for field in messages[-1].fields)
    assert "000708" in fields and "25" in fields and "timeout" in fields
    assert "4402" not in fields
    messages.clear()
    now[0] = 10.0
    output.heartbeat()
    assert all(message.task is None or message.task.key != "supplier_daily_qfq" for message in messages)
