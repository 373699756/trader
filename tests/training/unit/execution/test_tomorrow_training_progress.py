from __future__ import annotations

from dataclasses import replace

import pytest

from trader.training.application.tomorrow_training import TomorrowTrainingProgress, TrainingFitDetail, TrainingSampleDiagnostics, TrainingValidationProgress
from trader.training.entrypoints.tomorrow_training_progress import StderrTomorrowTrainingProgress
from trader.infra.progress_format import DisplayWidth
from trader.recommendation.domain.publication.models import Strategy

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


class _MutableClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_heartbeat_only_observes_the_current_stage_for_each_profile(capsys) -> None:
    clock = _MutableClock()
    output = StderrTomorrowTrainingProgress(monotonic=clock, command_label="train·V5")
    output.publish(TomorrowTrainingProgress("sample_index", "started", 0, 1))
    clock.now = 1
    output.publish(TomorrowTrainingProgress("target_statistics", "started", 0, 1))
    capsys.readouterr()
    output._emit_heartbeat(6)
    lines = _compact(capsys.readouterr().err).splitlines()
    assert len(lines) == 1 and "train·V5·标签统计" in lines[0]
    assert "样本索引" not in lines[0]


def test_scan_bounds_and_global_v1_fit_do_not_claim_industry_or_percentage(capsys) -> None:
    output = StderrTomorrowTrainingProgress(monotonic=_Clock(0, 0, 5, 10), command_label="train·V1")
    output.publish(TomorrowTrainingProgress("history_conversion", "running", 50, 10_000, 7, total_is_exact=False))
    output.publish(TomorrowTrainingProgress("cross_section_conversion", "running", 7, 10_000, 7, total_is_exact=False))
    output.publish(TomorrowTrainingProgress("model_fit", "started", 0, 1, strategy=Strategy.TOMORROW))
    text = _compact(capsys.readouterr().err)
    assert "扫描 50行 | 生成 7样本行" in text
    assert "生成 7样本行" in text and "任务 0/1" in text
    assert all(value not in text for value in ("10000", "%", "行业", "余约", "估算"))


def test_industry_count_uses_typed_unit_even_without_fit_detail(capsys) -> None:
    output = StderrTomorrowTrainingProgress(monotonic=_Clock(0, 0), command_label="train·V3")
    output.publish(TomorrowTrainingProgress("model_fit", "running", 12, 83, count_unit="industries"))
    assert "行业 12/83" in _compact(capsys.readouterr().err)


def test_observation_diagnostics_and_upper_bound_changes_do_not_reset_stalled_time(capsys) -> None:
    clock = _MutableClock()
    output = StderrTomorrowTrainingProgress(monotonic=clock, command_label="train·V5")
    detail = TrainingFitDetail("银行", 20_000, "training", 1, 10, 1, 10)
    event = TomorrowTrainingProgress("model_fit", "running", 2, 83, fit_detail=detail, count_unit="industries")
    output.publish(event)
    clock.now = 2
    output.publish(replace(
        event, total_units=84, sample_diagnostics=TrainingSampleDiagnostics(1, 0, 0, 0, 0),
        fit_detail=replace(detail, training_rows=21_000),
    ))
    capsys.readouterr()
    output._emit_heartbeat(5)
    assert "未推进 00:00:05" in _compact(capsys.readouterr().err)
    clock.now = 8
    output.publish(replace(event, fit_detail=replace(detail, completed_batches=2)))
    output._emit_heartbeat(10)
    assert "未推进" not in _compact(capsys.readouterr().err)


def test_validation_is_an_actual_running_phase_after_all_models_are_fitted(capsys) -> None:
    clock = _MutableClock()
    output = StderrTomorrowTrainingProgress(monotonic=clock, command_label="train·V5")
    output.publish(TomorrowTrainingProgress("model_fit", "started", 0, 3, strategy=Strategy.TOMORROW))
    clock.now = 5
    output.publish(TomorrowTrainingProgress(
        "model_fit", "running", 3, 3, 3, strategy=Strategy.TOMORROW,
        fit_detail=TrainingFitDetail("银行", 20_000, "completed"),
    ))
    capsys.readouterr()
    clock.now = 6
    update = TomorrowTrainingProgress(
        "model_fit", "running", 3, 3, 3, strategy=Strategy.TOMORROW,
        validation=TrainingValidationProgress(0, 100),
    )
    output.publish(update)
    lines = _compact(capsys.readouterr().err).splitlines()
    assert len(lines) == 1 and "明日·验证 00:00:00 | 处理中 | 验证 0/ 100行" in lines[0]
    output._emit_heartbeat(11)
    assert "明日·验证 00:00:05 | 等待 | 验证 0/ 100行" in _compact(capsys.readouterr().err)
    clock.now = 12
    output.publish(replace(update, state="completed", validation=TrainingValidationProgress(100, 100)))
    assert "明日·验证 00:00:06 | 完成 | 验证 100/ 100行" in _compact(capsys.readouterr().err)


def test_each_validation_head_and_original_stage_starts_its_own_clock_without_batch_flooding(capsys) -> None:
    clock = _MutableClock()
    output = StderrTomorrowTrainingProgress(monotonic=clock, command_label="train·V5")
    event = TomorrowTrainingProgress(
        "model_fit", "running", 3, 3, 3, strategy=Strategy.TOMORROW,
        count_unit="industries", validation=TrainingValidationProgress(0, 100),
    )
    for now, changed in (
        (1, event),
        (2, replace(event, strategy=Strategy.D25)),
        (3, replace(event, stage="final_model_refit")),
    ):
        clock.now = now
        output.publish(changed)
    lines = _compact(capsys.readouterr().err).splitlines()
    assert len(lines) == 3 and all("验证 00:00:00 | 处理中" in line for line in lines)
    assert "明日·验证" in lines[0] and "D25·验证" in lines[1]
    clock.now = 4
    output.publish(replace(event, validation=TrainingValidationProgress(50, 100)))
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("reset", ("epoch", "industry", "head", "stage", "phase"))
def test_v5_eta_uses_only_current_epoch_and_resets_on_context_change(capsys, reset) -> None:
    clock = _MutableClock()
    output = StderrTomorrowTrainingProgress(monotonic=clock, command_label="train·V5")
    detail = TrainingFitDetail("银行", 20_000, "training", 1, 10, 0, 10)
    update = TomorrowTrainingProgress("model_fit", "running", 2, 83, strategy=Strategy.TOMORROW, fit_detail=detail)
    output.publish(update)
    for now, batch in ((2, 2), (5, 4)):
        clock.now = now
        output.publish(replace(update, fit_detail=replace(detail, completed_batches=batch)))
    assert "本轮余约 00:00:07" in _compact(capsys.readouterr().err)
    clock.now = 10
    changed = {
        "epoch": replace(update, fit_detail=replace(detail, epoch=2)),
        "industry": replace(update, fit_detail=replace(detail, industry="软件")),
        "head": replace(update, strategy=Strategy.D25),
        "stage": replace(update, stage="final_model_refit"),
        "phase": replace(update, fit_detail=replace(detail, phase="calibration")),
    }[reset]
    output.publish(changed)
    text = _compact(capsys.readouterr().err)
    assert "本轮余约" not in text
    assert ("本轮估算中" in text) is (reset != "phase")


def test_training_columns_keep_display_positions_for_large_counts_and_long_industries(capsys) -> None:
    output = StderrTomorrowTrainingProgress(monotonic=_Clock(0, 0, 5, 10), command_label="train·V5")
    output.publish(TomorrowTrainingProgress("history_conversion", "started", 0, 1))
    output.publish(TomorrowTrainingProgress(
        "model_fit", "running", 30, 83, strategy=Strategy.TOMORROW,
        fit_detail=TrainingFitDetail("计算机通信和其他电子设备制造业", 123_456_789, "training", 10, 10, 12345, 12345),
    ))
    output.publish(TomorrowTrainingProgress("cross_section_conversion", "completed", 123_456_789, 123_456_789, 123_456_789))
    lines = capsys.readouterr().err.splitlines()
    separators = [[DisplayWidth(line[:match.start()]) for match in re.finditer(r"\|", line)] for line in lines]
    assert len(separators) == 3 and all(len(positions) == 6 for positions in separators)
    assert separators[0] == separators[1] == separators[2]
    assert "…" in lines[1] and "123456789" in lines[1]


@pytest.mark.parametrize("completed,total", ((-1, 2), (3, 2), (0, -1), (True, 1)))
def test_validation_progress_rejects_inaccurate_row_counts(completed, total) -> None:
    with pytest.raises(ValueError, match="training_validation_progress_invalid"):
        TrainingValidationProgress(completed, total)


def test_cache_sealing_has_its_own_clock_and_byte_progress(capsys):
    output = StderrTomorrowTrainingProgress(monotonic=_Clock(0, 0, 7200, 7210, 7211), start_heartbeat=False)
    output.publish(TomorrowTrainingProgress("sample_cache", "started", 0, 1))
    output.publish(TomorrowTrainingProgress("sample_cache_seal", "started", 0, 2 * 1024**2))
    output.publish(TomorrowTrainingProgress("sample_cache_seal", "running", 1024**2, 2 * 1024**2))
    output.publish(TomorrowTrainingProgress("sample_cache_seal", "completed", 2 * 1024**2, 2 * 1024**2))
    output_text = _compact(capsys.readouterr().err)
    assert "Tomorrow训练·缓存封存 00:00:10 | 处理中 | 1.0/2.0MiB" in output_text
    assert ("sample_cache_seal", 11.0) in output.stage_durations


def test_training_progress_separates_scanned_and_generated_rows_without_repeating_budget(capsys) -> None:
    output = StderrTomorrowTrainingProgress(monotonic=_Clock(100.0, 100.0, 854.0), start_heartbeat=False)

    output.publish(TomorrowTrainingProgress("resource_preflight", "completed", 1, 1))
    output.publish(TomorrowTrainingProgress("history_conversion", "running", 850, 5453, produced_units=612))

    lines = _compact(capsys.readouterr().err).splitlines()
    assert lines == [
        "00:00:00 | Tomorrow训练·资源预检 00:00:00 | 完成 | 任务 1/1 | — | — | —",
        "00:12:34 | Tomorrow训练·样本构建 00:00:00 | 处理中 | 扫描 850行 | 生成 612样本行 | — | —",
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
        "00:00:00 | Tomorrow训练·样本构建 00:00:00 | 开始 | 扫描 0行 | 生成 0样本行 | — | —",
        "00:00:10 | Tomorrow训练·样本构建 00:00:10 | 处理中 | 扫描 1行 | 生成 0样本行 | — | —",
        "00:00:31 | Tomorrow训练·样本构建 00:00:31 | 处理中 | 扫描 25行 | 生成 0样本行 | — | —",
        "00:00:32 | Tomorrow训练·样本构建 00:00:32 | 完成 | 扫描 100行 | 生成 0样本行 | — | —",
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
        "00:00:05 | Tomorrow训练·样本索引 00:00:05 | 等待 | 任务 0/1 | — | — | 未推进 00:00:05",
        "00:00:10 | Tomorrow训练·样本索引 00:00:10 | 等待 | 任务 0/1 | — | — | 未推进 00:00:10",
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
    assert "明日·拟合 00:00:42" in _compact(capsys.readouterr().err).splitlines()[-1]


def test_duplicate_completion_does_not_double_count_stage_duration() -> None:
    output = StderrTomorrowTrainingProgress(monotonic=_Clock(0, 0, 10, 15, 20), start_heartbeat=False)
    output.publish(TomorrowTrainingProgress("sample_index", "started", 0, 1))
    output.publish(TomorrowTrainingProgress("sample_index", "completed", 1, 1))
    output.publish(TomorrowTrainingProgress("target_statistics", "started", 0, 1))
    output.publish(TomorrowTrainingProgress("sample_index", "completed", 1, 1))
    assert output.stage_durations == (("sample_index", 10.0),)


def test_fit_detail_retains_latest_batch_and_defers_skips_to_terminal_summary(capsys) -> None:
    output = StderrTomorrowTrainingProgress(monotonic=_Clock(0, 0, 5, 5.1, 5.2, 11, 12, 16), start_heartbeat=False)
    output.publish(TomorrowTrainingProgress("model_fit", "started", 0, 83))
    detail = TrainingFitDetail("银行", 20000, "training", 2, 100, 0, 79)
    update = TomorrowTrainingProgress("model_fit", "running", 11, 83, fit_detail=detail)
    output.publish(update)
    assert "银行·训练 | 轮 2/100 批 0/79" in _compact(capsys.readouterr().err)
    for batch in (1, 2):
        output.publish(replace(update, fit_detail=replace(detail, completed_batches=batch)))
    assert _compact(capsys.readouterr().err) == ""
    output._emit_heartbeat(10)
    heartbeat = _compact(capsys.readouterr().err)
    assert "拟合 00:00:10 | 等待" in heartbeat and "批 2/79" in heartbeat
    skipped = TrainingFitDetail("银行", 20000, "skipped", failure_reason="calibration_prediction_variance_insufficient")
    output.publish(replace(update, completed_units=12, fit_detail=skipped))
    assert capsys.readouterr().err == ""
    output.publish(replace(update, completed_units=12, fit_detail=TrainingFitDetail("软件", 30000, "preparation")))
    assert capsys.readouterr().err == ""
    output._emit_heartbeat(15)
    assert "软件·检查" in _compact(capsys.readouterr().err)
    output.publish_result("engineering_ready", None)
    summary = _compact(capsys.readouterr().err)
    assert "跳过 1项" in summary and "calibration_prediction_variance_insufficient 1项" in summary


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
    assert all(line.startswith("00:00:00 | ") and "生成 0样本行" in line and len(line.split(" | ")) == 7 for line in lines)
    assert sorted(line.split(" | ")[1] for line in lines) == [
        "V1训练·样本构建 00:00:00", "V2训练·样本构建 00:00:00",
    ]


def test_profiles_share_command_clock_and_cancel_retains_last_confirmed_progress(capsys):
    output = StderrTomorrowTrainingProgress(monotonic=_Clock(100, 105, 110), started_at=0, start_heartbeat=False, command_label="train·V5")
    output.publish(TomorrowTrainingProgress("model_fit", "running", 3, 83, count_unit="industries"))
    output.publish_cancelled()
    lines = _compact(capsys.readouterr().err).splitlines()
    assert lines[0].startswith("00:01:45 | train·V5·拟合 00:00:00 | 处理中 | 行业 3/83")
    assert "取消 | 最后阶段 拟合 行业 3/83" in lines[-1]
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
