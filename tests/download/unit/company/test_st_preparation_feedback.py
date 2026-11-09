"""ST qualification keeps processed, durable, and admitted counts distinct."""

from dataclasses import FrozenInstanceError
from io import StringIO

import pytest

from trader.download.domain.history_sync import (
    HistoryStCheckFailure, HistoryStCheckProgress, HistoryStEligibilitySummary,
)
from trader.download.entrypoints.company_evidence_progress import CompanyEvidenceProgress, ReportStPreparation


def _text(message):
    return " | ".join(field.text for field in message.fields)


def test_intermediate_st_feedback_never_labels_processed_checks_as_saved():
    messages = []
    observation = HistoryStCheckProgress(8, 0, 80)
    ReportStPreparation(CompanyEvidenceProgress(report=messages.append, monotonic=lambda: 0), observation)
    assert len(messages) == 1
    line = _text(messages[0])
    assert "已核验 8/80股" in line and "已保存 0/80股" in line
    assert "来源 新浪" in line and "持久化" not in line
    assert not messages[0].finished


def test_failed_stock_is_immediate_before_aggregate_and_does_not_hide_its_evidence():
    messages = []
    failure = HistoryStCheckFailure("300101", "示例股票", "http_timeout", "HttpTimeout:history_st_source.py:_fetch_code:200",
                                    2, 2, 29.75, 15.0)
    observation = HistoryStCheckProgress(1, 0, 2, failures=(failure,))
    ReportStPreparation(CompanyEvidenceProgress(report=messages.append, monotonic=lambda: 0), observation)
    assert len(messages) == 2
    first, aggregate = map(_text, messages)
    assert "股票 300101 示例股票" in first and "原因码 http_timeout" in first
    assert "history_st_source.py:_fetch_code:200" in first and "尝试 2/2" in first
    assert "核验累计 29.750秒" in first and "单次超时上限 15.000秒" in first
    assert "待核unknown，继续下载" in first and "不阻断本轮同步" in first and "下次ST核验重查" in first
    assert messages[0].immediate and messages[0].state == "失败"
    assert "已核验 1/2股" in aggregate and "已保存 0/2股" in aggregate


def test_empty_history_prints_pending_download_instead_of_exception_diagnostics():
    messages = []
    failure = HistoryStCheckFailure(
        "688055", "龙腾光电", "history_st_name_history_empty",
        "ValueError:history_st_source.py:ParseNameHistorySt:45", 1, 2, 0.067, 15.0,
    )
    ReportStPreparation(CompanyEvidenceProgress(report=messages.append, monotonic=lambda: 0),
                        HistoryStCheckProgress(1, 0, 2, failures=(failure,)))
    message = messages[0]
    line = _text(message)
    assert message.task.label == "ST待核" and message.state == "等待"
    assert "股票 688055 龙腾光电" in line and "原因 曾用名历史为空" in line
    assert "待核unknown，继续下载" in line and "下次ST核验重查" in line
    assert "诊断" not in line and "ValueError" not in line and "超时" not in line
    assert message.stock_failure_code is None and message.finished and not message.heartbeat


def test_st_summary_with_unknowns_reports_continued_download_without_faking_verified_status():
    stream = StringIO()
    progress = CompanyEvidenceProgress(stream, monotonic=lambda: 0)
    ReportStPreparation(progress, HistoryStCheckProgress(5224, 5224, 5224, HistoryStEligibilitySummary(4402, 820, 2)))
    line = stream.getvalue()
    assert "参考·ST资格" in line and "已核验 5224/5224股" in line and "已保存 5224/5224股" in line
    assert "已核实非ST 4402股" in line and "当前或曾ST排除 820股" in line and "待核继续下载 2股" in line
    assert "结果 核验结束，待核股票继续下载" in line
    assert "unknown:" not in line and "原因 unknown" not in line and "结果 完成" not in line


def test_cached_st_feedback_names_the_original_source_and_typed_observation():
    events, messages = [], []
    observation = HistoryStCheckProgress(2, 2, 2, HistoryStEligibilitySummary(1, 1, 0), cache_only=True)
    ReportStPreparation(events.append, observation)
    assert len(events) == 1 and events[0].st_check is observation and events[0].failure == ""
    CompanyEvidenceProgress(report=messages.append, monotonic=lambda: 0)(events[0])
    assert "来源 本地缓存（原来源 新浪）" in _text(messages[0])
    assert "待核继续下载 0股" in _text(messages[0]) and messages[0].finished
    with pytest.raises(FrozenInstanceError):
        observation.persisted = 0


@pytest.mark.parametrize("processed,persisted,total,summary", [
    (1, 2, 2, None), (3, 1, 2, None), (2, 1, 2, HistoryStEligibilitySummary(1, 0, 1)),
])
def test_st_observation_rejects_unconfirmed_durability_or_premature_final_counts(processed, persisted, total, summary):
    with pytest.raises(ValueError, match="history_st_check_progress_invalid"):
        HistoryStCheckProgress(processed, persisted, total, summary)
