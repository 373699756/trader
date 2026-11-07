from __future__ import annotations

from dataclasses import replace

import pytest

from trader.training.application.tomorrow_training import TomorrowTrainingProgress, TrainingFitDetail
from trader.training.entrypoints.tomorrow_training_progress import StderrTomorrowTrainingProgress

import io
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor


def _compact(text: str) -> str:
    return re.sub(r" +", " ", text)


class _Clock:
    def __init__(self, *values: float) -> None:
        self._values = iter(values)

    def __call__(self) -> float:
        return next(self._values)


def test_cache_sealing_has_its_own_clock_and_byte_progress(capsys):
    output = StderrTomorrowTrainingProgress(monotonic=_Clock(0, 0, 7200, 7210, 7211), start_heartbeat=False)
    output.publish(TomorrowTrainingProgress("sample_cache", "started", 0, 1))
    output.publish(TomorrowTrainingProgress("sample_cache_seal", "started", 0, 2 * 1024**2))
    output.publish(TomorrowTrainingProgress("sample_cache_seal", "running", 1024**2, 2 * 1024**2))
    output.publish(TomorrowTrainingProgress("sample_cache_seal", "completed", 2 * 1024**2, 2 * 1024**2))
    output_text = _compact(capsys.readouterr().err)
    assert "Tomorrow训练·样本缓存封存 00:00:10 | 处理中 | 1.0/2.0 MiB (50.00%)" in output_text
    assert ("sample_cache_seal", 11.0) in output.stage_durations


def test_training_progress_prints_real_rows_percent_duration_and_no_rss(capsys) -> None:
    output = StderrTomorrowTrainingProgress(monotonic=_Clock(100.0, 100.0, 854.0), start_heartbeat=False)

    output.publish(TomorrowTrainingProgress("resource_preflight", "completed", 1, 1))
    output.publish(TomorrowTrainingProgress("history_conversion", "running", 850, 5453, produced_units=612))

    lines = _compact(capsys.readouterr().err).splitlines()
    assert lines == [
        "00:00:00 | Tomorrow训练·资源预检 00:00:00 | 完成 | 1/1 (100.00%) "
        "| 峰值 RSS 上限 2048 MiB",
        "00:12:34 | Tomorrow训练·历史读取与训练样本生成 00:00:00 | 处理中 | 850/5453 (15.59%) | 已生成样本 612",
    ]


def test_training_progress_throttles_advances_but_emits_stage_boundaries(capsys) -> None:
    output = StderrTomorrowTrainingProgress(
        monotonic=_Clock(100.0, 100.0, 110.0, 131.0, 132.0),
        start_heartbeat=False,
    )

    output.publish(TomorrowTrainingProgress("history_conversion", "started", 0, 100))
    output.publish(TomorrowTrainingProgress("history_conversion", "running", 1, 100))
    output.publish(TomorrowTrainingProgress("history_conversion", "running", 25, 100))
    output.publish(TomorrowTrainingProgress("history_conversion", "completed", 100, 100))

    assert _compact(capsys.readouterr().err).splitlines() == [
        "00:00:00 | Tomorrow训练·历史读取与训练样本生成 00:00:00 | 开始 | 0/100 (0.00%) | 已生成样本 0",
        "00:00:10 | Tomorrow训练·历史读取与训练样本生成 00:00:10 | 处理中 | 1/100 (1.00%) | 已生成样本 0",
        "00:00:31 | Tomorrow训练·历史读取与训练样本生成 00:00:31 | 处理中 | 25/100 (25.00%) | 已生成样本 0",
        "00:00:32 | Tomorrow训练·历史读取与训练样本生成 00:00:32 | 完成 | 100/100 (100.00%) | 已生成样本 0",
    ]
    assert output.stage_durations == (("history_conversion", 32.0),)


def test_training_progress_prints_compact_blocked_and_cancelled_results(capsys) -> None:
    output = StderrTomorrowTrainingProgress(
        monotonic=_Clock(100.0, 161.0, 162.0),
        start_heartbeat=False,
    )

    output.publish_result("blocked", "history_maintenance_running")
    output.publish_cancelled()

    assert _compact(capsys.readouterr().err).splitlines() == [
        "00:01:01 | Tomorrow训练·结果汇总 00:00:00 | 阻塞 | 状态 blocked | 错误 history_maintenance_running",
        "00:01:02 | Tomorrow训练·结果汇总 00:00:01 | 取消",
    ]


def test_expensive_index_stage_emits_heartbeat_at_five_seconds(capsys) -> None:
    output = StderrTomorrowTrainingProgress(monotonic=_Clock(0.0, 0.0, 61.0), start_heartbeat=False)
    output.publish(TomorrowTrainingProgress("sample_index", "started", 0, 1))
    capsys.readouterr()
    output._emit_heartbeat(4.0)
    assert _compact(capsys.readouterr().err) == ""
    output._emit_heartbeat(5.0)
    output._emit_heartbeat(10.0)
    assert _compact(capsys.readouterr().err).splitlines() == [
        "00:00:05 | Tomorrow训练·样本索引 00:00:05 | 等待 | 0/1 (0.00%) | 距上次进度更新 00:00:05",
        "00:00:10 | Tomorrow训练·样本索引 00:00:10 | 等待 | 0/1 (0.00%) | 距上次进度更新 00:00:10",
    ]
    output.publish_result("engineering_ready", None)
    capsys.readouterr()
    output._emit_heartbeat(120.0)
    assert _compact(capsys.readouterr().err) == ""


def test_repeated_head_stages_keep_display_clock_and_sum_evidence(capsys) -> None:
    from trader.recommendation.domain.publication.models import Strategy

    output = StderrTomorrowTrainingProgress(monotonic=_Clock(0, 0, 10, 20, 25, 40, 42), start_heartbeat=False)
    for strategy in (Strategy.TOMORROW, Strategy.D25, Strategy.TOMORROW):
        output.publish(TomorrowTrainingProgress("model_fit", "started", 0, 1, strategy=strategy))
        output.publish(TomorrowTrainingProgress("model_fit", "completed", 1, 1, strategy=strategy))
    assert output.stage_durations == (("model_fit", 17.0),)
    assert "tomorrow·行业模型 00:00:42" in _compact(capsys.readouterr().err).splitlines()[-1]


def test_duplicate_completion_does_not_double_count_stage_duration() -> None:
    output = StderrTomorrowTrainingProgress(monotonic=_Clock(0, 0, 10, 15, 20), start_heartbeat=False)
    output.publish(TomorrowTrainingProgress("sample_index", "started", 0, 1))
    output.publish(TomorrowTrainingProgress("sample_index", "completed", 1, 1))
    output.publish(TomorrowTrainingProgress("target_statistics", "started", 0, 1))
    output.publish(TomorrowTrainingProgress("sample_index", "completed", 1, 1))
    assert output.stage_durations == (("sample_index", 10.0),)


def test_fit_detail_retains_latest_batch_without_flooding_and_emits_skips_immediately(capsys) -> None:
    output = StderrTomorrowTrainingProgress(monotonic=_Clock(0, 0, 1, 1.1, 1.2, 11, 12), start_heartbeat=False)
    output.publish(TomorrowTrainingProgress("model_fit", "started", 0, 83))
    detail = TrainingFitDetail("银行", 20000, "training", 2, 100, 0, 79)
    update = TomorrowTrainingProgress("model_fit", "running", 11, 83, fit_detail=detail)
    output.publish(update)
    assert "行业 银行 | 训练样本 20000 | 训练 | 轮次 2/100 | 已完成批次 0/79" in _compact(capsys.readouterr().err)
    for batch in (1, 2):
        output.publish(replace(update, fit_detail=replace(detail, completed_batches=batch)))
    assert _compact(capsys.readouterr().err) == ""
    output._emit_heartbeat(11)
    heartbeat = _compact(capsys.readouterr().err)
    assert "行业模型 00:00:11 | 等待" in heartbeat and "已完成批次 2/79" in heartbeat
    assert "距上次进度更新 00:00:09" in heartbeat
    skipped = TrainingFitDetail("银行", 20000, "skipped", failure_reason="calibration_prediction_variance_insufficient")
    output.publish(replace(update, completed_units=12, fit_detail=skipped))
    assert "行业跳过 | 原因 calibration_prediction_variance_insufficient" in _compact(capsys.readouterr().err)
    output.publish(replace(update, completed_units=12, fit_detail=TrainingFitDetail("软件", 30000, "preparation")))
    assert "行业 软件 | 训练样本 30000 | 样本检查" in _compact(capsys.readouterr().err)


@pytest.mark.parametrize(
    "detail",
    (
        {"epoch": 2, "total_epochs": 1},
        {"completed_batches": 2, "total_batches": 1},
        {"training_rows": -1},
        {"phase": "skipped"},
        {"failure_reason": "invalid_while_training"},
    ),
)
def test_fit_detail_rejects_impossible_progress(detail) -> None:
    values = {"industry": "银行", "training_rows": 20000, "phase": "training", **detail}
    with pytest.raises(ValueError, match="fit detail"):
        TrainingFitDetail(**values)


def test_parallel_profiles_keep_complete_lines_when_the_stream_yields_between_writes(monkeypatch) -> None:
    barrier = threading.Barrier(2)

    class YieldingStream(io.StringIO):
        def write(self, text: str) -> int:
            count = super().write(text)
            if text:
                barrier.wait(timeout=3)
            return count

    stream = YieldingStream()
    monkeypatch.setattr(sys, "stderr", stream)
    outputs = tuple(
        StderrTomorrowTrainingProgress(monotonic=lambda: 0.0, start_heartbeat=False, command_label=label)
        for label in ("V1训练", "V2训练")
    )
    update = TomorrowTrainingProgress("history_conversion", "started", 0, 100)
    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(lambda output: output.publish(update), outputs))
    lines = [_compact(line) for line in stream.getvalue().splitlines()]
    assert len(lines) == 2
    assert all(line.startswith("00:00:00 | ") and line.endswith("已生成样本 0") for line in lines)
    assert sorted(line.split(" | ")[1] for line in lines) == [
        "V1训练·历史读取与训练样本生成 00:00:00", "V2训练·历史读取与训练样本生成 00:00:00",
    ]


def test_profiles_share_command_clock_and_cancel_retains_last_confirmed_progress(capsys):
    output = StderrTomorrowTrainingProgress(monotonic=_Clock(100, 105, 110), started_at=0, start_heartbeat=False, command_label="train·V5")
    output.publish(TomorrowTrainingProgress("model_fit", "running", 3, 83))
    output.publish_cancelled()
    lines = _compact(capsys.readouterr().err).splitlines()
    assert lines[0].startswith("00:01:45 | train·V5·行业模型 00:00:00 | 处理中 | 3/83")
    assert lines[-1].endswith("取消 | 最后阶段 行业模型 3/83")
    output._emit_heartbeat(120)
    assert _compact(capsys.readouterr().err) == ""


def test_stage_notice_heartbeat_preserves_waiting_operation_and_stops_when_finished(capsys):
    output = StderrTomorrowTrainingProgress(monotonic=_Clock(0, 1, 8), start_heartbeat=False, command_label="train")
    output.report_notice("配置检查", "读取本地配置", state="开始")
    output._emit_heartbeat(6)
    assert "00:00:06 | train·配置检查 00:00:05 | 等待 | 读取本地配置" in _compact(capsys.readouterr().err)
    output.report_notice("配置检查", "已完成", state="完成")
    capsys.readouterr()
    output._emit_heartbeat(20)
    assert _compact(capsys.readouterr().err) == ""


def test_blocked_heartbeat_uses_the_training_scope_deadline_and_preserves_incomplete_step(monkeypatch):
    from threading import Event, current_thread
    from trader.infra.shutdown import ShutdownDeadline
    from trader.infra.signal_cancellation import ManualCancellation

    blocked = Event()
    release = Event()
    forced = Event()
    caller = current_thread()
    clock = [0.0]

    class BlockingStream:
        def write(self, text):
            if current_thread() is not caller:
                blocked.set()
                assert release.wait(2)
            return len(text)

        def flush(self):
            return None

    monkeypatch.setattr(sys, "stderr", BlockingStream())
    with ManualCancellation(timeout_seconds=0.05, force_exit=lambda _code: forced.set()) as scope:
        output = StderrTomorrowTrainingProgress(
            monotonic=lambda: clock[0], start_heartbeat=True, cancellation=scope,
        )
        output._heartbeat._interval = 0.01
        try:
            with pytest.raises(TimeoutError, match="training_progress_shutdown_incomplete"):
                with output:
                    output.publish(TomorrowTrainingProgress("sample_index", "started", 0, 1))
                    clock[0] = 6.0
                    assert blocked.wait(2)
            assert output.shutdown_step is not None
            assert not output.shutdown_step.completed and output.shutdown_step.timed_out
            assert scope.shutdown_deadline() is scope._controller.deadline
            assert not scope.shutdown_completed
            assert forced.wait(2)
        finally:
            release.set()
            assert output._heartbeat.stop(ShutdownDeadline.start(2)).completed
        assert not scope.shutdown_completed
