"""Market progress reflects the selected provider and verified local provenance."""

from datetime import date

import pytest

from tests.helpers.download.history.qfq_windows import DAYS, Supplier, _updater
from trader.download.application.qfq_download_progress import QfqDownloadProgress, QfqStockFeedback
from trader.download.application.progress_events import QfqNotice, QfqSummary
from trader.download.application.stock_download_progress import StockDownloadProgress
from trader.download.infra.progress_projection import FormatStockDownloadProgress, ProjectingDownloadProgress
from trader.download.entrypoints.sync_progress import SyncProgress
from trader.download.domain.baostock_daily import BaoStockSecurity
from trader.download.domain.qfq_planning import QfqPlanningSnapshot, QfqPlanningState
from trader.download.domain.qfq_window import QfqWindowSnapshot, QfqWindowState


def test_qfq_application_emits_unformatted_facts_for_the_output_adapter():
    events = []
    progress = QfqDownloadProgress(date(2026, 11, 6), "tencent-newfqkline:digest", 1, events.append)
    progress.publish("价格更新", ("包含 | 分隔符的单条事实",), source=progress.source)
    progress.summary("汇总")

    assert isinstance(events[0], QfqNotice)
    assert events[0].details == ("包含 | 分隔符的单条事实",)
    assert events[0].source == "tencent-newfqkline:digest"
    assert isinstance(events[1], QfqSummary)
    assert events[1].total == 1 and events[1].processed == 0
    with pytest.raises(TypeError, match="download_progress_details_require_typed_tuple"):
        QfqNotice("价格更新", "不再接受旧拼接字符串")


@pytest.mark.parametrize("action", ("Tencent下载", "BaoStock补缺", "本地检查", "本地复用", "配对落库", "无需下载"))
def test_market_stock_label_follows_the_actual_action_without_changing_task_key(action):
    messages = []
    progress = QfqDownloadProgress(date(2026, 11, 6), "tencent-newfqkline:digest", 1, ProjectingDownloadProgress(messages.append))
    security = BaoStockSecurity("600001", "测试股份", "main", date(2000, 1, 1), None, "fixture")

    progress.record_stock(security, QfqStockFeedback(action, "合格", 1, 1, 1, 1))

    assert messages[0].task.owner == "market"
    assert messages[0].task.key == "价格更新"
    assert messages[0].task.label == action
    assert not any(field.text.startswith("原来源 ") for field in messages[0].fields)


@pytest.mark.parametrize(
    "source,expected",
    (("tencent-newfqkline:digest", "Tencent"), ("baostock:tencent-newfqkline:digest", "BaoStock"), ("fixture-provider:digest", "fixture-provider")),
)
def test_complete_local_plan_reports_verified_original_provider_without_requesting(tmp_path, source, expected):
    supplier = Supplier()
    updater = _updater(tmp_path, supplier)
    messages = []
    current_source = source.removeprefix("baostock:")
    progress = QfqDownloadProgress(DAYS[-1], current_source, 1, ProjectingDownloadProgress(messages.append))
    security = BaoStockSecurity("600001", "测试股份", "main", DAYS[0], None, "fixture")
    dates = DAYS[-updater.configuration.maximum_sessions:]
    v2 = QfqWindowSnapshot((QfqWindowState(security.code, source, dates, True, dates[0], dates[-1]),))
    v3_dates = dates[-updater.configuration.v3_sessions:]
    v3 = QfqWindowSnapshot((QfqWindowState(security.code, source, v3_dates, True, v3_dates[0], v3_dates[-1]),))
    planning = QfqPlanningSnapshot(v2, (QfqPlanningState(security.code, source, DAYS[-1], source, None, ()),))

    plan = updater._prepare_plan(security, dates, progress, planning, v3, DAYS[-1])

    assert plan is None and supplier.calls == []
    assert progress.completed == progress.skipped == 1
    assert messages[0].task.label == "本地复用"
    assert any(field.text == f"原来源 {expected}" for field in messages[0].fields)
    assert not any("处理 Tencent下载" in field.text for field in messages[0].fields)


def test_market_supplier_stage_label_preserves_price_task_identity():
    messages = []
    progress = QfqDownloadProgress(date(2026, 11, 6), "tencent-newfqkline:digest", 1, ProjectingDownloadProgress(messages.append))

    progress.publish("价格更新", ('请求进行中',), source=progress.source)

    assert messages[0].task.key == "价格更新" and messages[0].task.label == "Tencent下载"


def test_supplier_label_switch_keeps_elapsed_time_and_one_latest_source_heartbeat():
    clock = [0.0]
    messages = []
    report = SyncProgress(messages.append, lambda: clock[0])
    progress = QfqDownloadProgress(date(2026, 11, 6), "tencent-newfqkline:digest", 1, report)
    progress.publish("价格更新", ('来源 Tencent',), state="开始", label="Tencent下载")
    first_task = messages[0].task
    clock[0] = 0.2
    progress.publish("价格更新", ('来源 BaoStock',), label="BaoStock补缺")
    assert len(messages) == 1

    clock[0] = 5.0
    report.heartbeat()
    report.heartbeat()

    assert len(messages) == 2
    heartbeat = messages[-1]
    assert heartbeat.task == first_task and heartbeat.task.execution == first_task.execution
    assert heartbeat.state == "等待" and heartbeat.task.label == "BaoStock补缺"
    assert heartbeat.fields[1].text.split() == ["market·BaoStock补缺", "00:00:05"]
    assert any(field.text == "来源 BaoStock" for field in heartbeat.fields)
    clock[0] = 10.0
    report.heartbeat()
    assert len(messages) == 3 and messages[-1].task.label == "BaoStock补缺"


def test_history_stock_stage_keeps_tencent_download_label():
    message = FormatStockDownloadProgress(StockDownloadProgress(
        "sync history", 1, 1, "600001", "测试股份", "本地复用", "复用", 1, 1, 1, 1, 0,
    ))

    assert message.task.owner == "history" and message.task.label == "Tencent下载"
