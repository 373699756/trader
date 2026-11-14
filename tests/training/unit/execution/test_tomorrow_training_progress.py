from __future__ import annotations

from dataclasses import replace

import pytest

from trader.training.application.tomorrow_training import TomorrowTrainingProgress, TrainingFitCounts, TrainingFitDetail, TrainingSampleDiagnostics, TrainingValidationProgress
from trader.training.entrypoints.tomorrow_training_progress import (
    ReadTrainingProgressLayout,
    StderrTomorrowTrainingProgress,
    TrainingProgressLayout,
)
from trader.infra.progress_format import DisplayWidth
from trader.recommendation.domain.publication.models import Strategy

import io
import os
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
    output._emit_heartbeat(31)
    lines = _compact(capsys.readouterr().err).splitlines()
    assert len(lines) == 1 and "train·V5·标签统计" in lines[0]
    assert "样本索引" not in lines[0]


def test_scan_bounds_and_global_v1_fit_do_not_claim_industry_or_percentage(capsys) -> None:
    output = StderrTomorrowTrainingProgress(monotonic=_Clock(0, 0, 5, 10), command_label="train·V1")
    output.publish(TomorrowTrainingProgress("history_conversion", "running", 50, 10_000, 7, total_is_exact=False))
    output.publish(TomorrowTrainingProgress("cross_section_conversion", "running", 7, 10_000, 7, total_is_exact=False))
    output.publish(TomorrowTrainingProgress("model_fit", "started", 0, 1, strategy=Strategy.TOMORROW))
    text = _compact(capsys.readouterr().err)
    assert "扫描 50行；生成 7样本行" in text
    assert "生成 7样本行" in text and "任务 0/1" in text
    assert all(value not in text for value in ("10000", "%", "行业", "余约", "估算"))


def test_industry_count_uses_typed_unit_even_without_fit_detail(capsys) -> None:
    output = StderrTomorrowTrainingProgress(monotonic=_Clock(0, 0), command_label="train·V3")
    output.publish(TomorrowTrainingProgress("model_fit", "running", 12, 83, count_unit="industries"))
    assert "行业 12/83" in _compact(capsys.readouterr().err)


def test_observation_diagnostics_and_upper_bound_changes_do_not_reset_stalled_time(capsys) -> None:
    clock = _MutableClock()
    output = StderrTomorrowTrainingProgress(monotonic=clock, command_label="train·V5")
    detail = TrainingFitDetail("银行", 20_000, "training", 1, 10, 1, 10, industry_name="银行")
    event = TomorrowTrainingProgress("model_fit", "running", 2, 83, fit_detail=detail, count_unit="industries")
    output.publish(event)
    clock.now = 2
    output.publish(replace(
        event, total_units=84, sample_diagnostics=TrainingSampleDiagnostics(1, 0, 0, 0, 0),
        fit_detail=replace(detail, training_rows=21_000),
    ))
    capsys.readouterr()
    output._emit_heartbeat(30)
    assert "未推进 00:00:30" in _compact(capsys.readouterr().err)
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
        fit_detail=TrainingFitDetail("银行", 20_000, "completed", industry_name="银行"),
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
    output._emit_heartbeat(36)
    assert "明日·验证 00:00:30 | 等待 | 验证 0/ 100行" in _compact(capsys.readouterr().err)
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
    detail = TrainingFitDetail("银行", 20_000, "training", 1, 10, 0, 10, industry_name="银行")
    update = TomorrowTrainingProgress("model_fit", "running", 2, 83, strategy=Strategy.TOMORROW, fit_detail=detail)
    output.publish(update)
    for now, batch in ((2, 2), (5, 4)):
        clock.now = now
        output.publish(replace(update, fit_detail=replace(detail, completed_batches=batch)))
    assert "本轮余约 00:00:07" in _compact(capsys.readouterr().err)
    clock.now = 10
    changed = {
        "epoch": replace(update, fit_detail=replace(detail, epoch=2)),
        "industry": replace(update, fit_detail=replace(detail, industry="软件", industry_name="软件")),
        "head": replace(update, strategy=Strategy.D25),
        "stage": replace(update, stage="final_model_refit"),
        "phase": replace(update, fit_detail=replace(detail, phase="calibration")),
    }[reset]
    output.publish(changed)
    text = _compact(capsys.readouterr().err)
    assert "本轮余约" not in text
    assert ("本轮估算中" in text) is (reset != "phase")


def test_training_prefixes_stay_compact_with_large_counts_and_long_industries(capsys) -> None:
    output = StderrTomorrowTrainingProgress(monotonic=_Clock(0, 0, 5, 10), command_label="train·V5")
    output.publish(TomorrowTrainingProgress("history_conversion", "started", 0, 1))
    output.publish(TomorrowTrainingProgress(
        "model_fit", "running", 30, 83, strategy=Strategy.TOMORROW,
        fit_detail=TrainingFitDetail(
            "计算机通信和其他电子设备制造业", 123_456_789, "training", 10, 10, 12345, 12345,
            industry_name="计算机通信和其他电子设备制造业",
        ),
    ))
    output.publish(TomorrowTrainingProgress("cross_section_conversion", "completed", 123_456_789, 123_456_789, 123_456_789))
    lines = capsys.readouterr().err.splitlines()
    main_lines = [line for line in lines if " | " in line]
    separators = [[DisplayWidth(line[:match.start()]) for match in re.finditer(r"\|", line)] for line in main_lines]
    assert len(separators) == 3 and all(len(positions) == 3 for positions in separators)
    assert all(part == part.strip() for line in main_lines for part in line.split(" | ")[:3])
    assert all(DisplayWidth(line) <= 180 for line in lines)
    assert "计算机通信和其他电子设备制造业" in "".join(line.strip() for line in lines)
    assert "…" not in "".join(lines) and "123456789" in "".join(lines)


def test_head_and_batch_percentages_use_actual_counts_without_treating_epoch_as_overall(capsys):
    output = StderrTomorrowTrainingProgress(monotonic=_Clock(0, 0), command_label="train·V5")
    output.publish(TomorrowTrainingProgress(
        "model_fit", "running", 2, 10, 1, strategy=Strategy.TOMORROW, count_unit="industries",
        fit_counts=TrainingFitCounts(2, 10, 1, 1),
        fit_detail=TrainingFitDetail(
            "capco:C39计算机通信和其他电子设备制造业", 20000, "training", 6, 10, 8, 10,
            industry_name="计算机通信和其他电子设备制造业",
        ),
    ))
    text = "".join(line.strip() for line in capsys.readouterr().err.splitlines())
    assert "行业 2/10 (20.0%)" in text and "成功 1 跳过 1" in text
    assert "轮次 6/10" in text and "本轮批次 8/10 (80.0%)" in text
    assert "60.0%" not in text and "capco:" not in text
    assert "计算机通信和其他电子设备制造业" in text


def test_fit_total_changes_alone_do_not_reset_stalled_time(capsys):
    clock = _MutableClock()
    output = StderrTomorrowTrainingProgress(monotonic=clock)
    event = TomorrowTrainingProgress(
        "model_fit", "running", 1, 4, 1, count_unit="industries", fit_counts=TrainingFitCounts(1, 4, 1, 0),
    )
    output.publish(event)
    clock.now = 2
    output.publish(replace(event, total_units=5, fit_counts=TrainingFitCounts(1, 5, 1, 0)))
    capsys.readouterr()
    output._emit_heartbeat(30)
    assert "未推进 00:00:30" in _compact(capsys.readouterr().err)


def test_fit_upper_bound_does_not_claim_an_exact_percentage(capsys):
    output = StderrTomorrowTrainingProgress(monotonic=_Clock(0, 0))
    output.publish(TomorrowTrainingProgress(
        "model_fit", "running", 2, 100, 1, count_unit="industries", total_is_exact=False,
        fit_counts=TrainingFitCounts(2, 100, 1, 1),
    ))
    text = capsys.readouterr().err
    assert "行业已处理 2" in text and "成功 1 跳过 1" in text
    assert "%" not in text and "100" not in text


@pytest.mark.parametrize("identity", ("capco:J66货币金融服务", "internal_sector_identity"))
def test_unknown_industry_name_never_falls_back_to_an_internal_identity(capsys, identity):
    output = StderrTomorrowTrainingProgress(monotonic=_Clock(0, 0))
    output.publish(TomorrowTrainingProgress(
        "model_fit", "running", 1, 83, fit_detail=TrainingFitDetail(identity, 20000, "training"),
    ))
    text = capsys.readouterr().err
    assert identity not in text and "货币金融服务" not in text
    assert "样本 20000行" in text


@pytest.mark.parametrize("width", (80, 120, 180))
def test_wrapped_native_name_and_complete_error_use_one_atomic_write_and_no_empty_columns(monkeypatch, width):
    class RecordingStream(io.StringIO):
        writes = 0
        flushes = 0

        def write(self, text):
            self.writes += 1
            return super().write(text)

        def flush(self):
            self.flushes += 1

    stream = RecordingStream()
    monkeypatch.setattr(sys, "stderr", stream)
    output = StderrTomorrowTrainingProgress(monotonic=_Clock(0, 0, 1), layout=TrainingProgressLayout(width))
    name = "计算机通信和其他电子设备制造业"
    output.publish(TomorrowTrainingProgress(
        "model_fit", "running", 1, 83, fit_detail=TrainingFitDetail(
            "capco:C39" + name, 123456789, "training", 6, 10, 9, 10, industry_name=name,
        ),
    ))
    error = "calibration_prediction_variance_insufficient_" * 4
    output.publish_result("failed", error)
    lines = stream.getvalue().splitlines()
    main = [line for line in lines if " | " in line]
    assert len(main) == 2 and all(len(line.split(" | ")) == 4 for line in main)
    assert all(DisplayWidth(line) <= width for line in lines)
    offset = DisplayWidth(main[0].rsplit(" | ", 1)[0]) + 3
    assert all(line.startswith(" " * offset) for line in lines if line not in main)
    rendered = "".join(line.strip() for line in lines)
    assert name in rendered and error in rendered
    assert all(value not in rendered for value in ("capco:", "…", "—", " |  | "))
    assert stream.writes == stream.flushes == 2


def test_layout_detects_stderr_once_and_is_shared_explicitly_across_profiles(monkeypatch):
    class TerminalStream(io.StringIO):
        def fileno(self):
            return 17

    stream = TerminalStream()
    monkeypatch.setattr(sys, "stderr", stream)
    descriptors = []

    def terminal_size(descriptor):
        descriptors.append(descriptor)
        return os.terminal_size((96, 24))

    monkeypatch.setattr(os, "get_terminal_size", terminal_size)
    layout = ReadTrainingProgressLayout()
    outputs = tuple(StderrTomorrowTrainingProgress(monotonic=lambda: 0, layout=layout) for _ in range(4))
    for output in outputs:
        output.report_notice("资源预检", "本地配置检查", state="完成")
    assert descriptors == [17]
    assert all(output.layout is layout and output.layout.columns == 96 for output in outputs)


def test_non_terminal_layout_falls_back_to_180_without_reading_stdout(monkeypatch):
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    monkeypatch.setattr(os, "get_terminal_size", lambda *_: pytest.fail("stderr has no terminal descriptor"))
    assert ReadTrainingProgressLayout().columns == 180


def test_subsecond_unfinished_epoch_eta_never_prints_zero_remaining(capsys):
    clock = _MutableClock()
    output = StderrTomorrowTrainingProgress(monotonic=clock)
    detail = TrainingFitDetail("银行", 20000, "training", 6, 10, 0, 100, industry_name="银行")
    update = TomorrowTrainingProgress("model_fit", "running", 1, 83, fit_detail=detail)
    output.publish(update)
    capsys.readouterr()
    clock.now = 5
    output.publish(replace(update, fit_detail=replace(detail, completed_batches=99)))
    text = "".join(line.strip() for line in capsys.readouterr().err.splitlines())
    assert "本轮余约 <1秒" in text and "本轮余约 00:00:00" not in text


def test_first_observation_of_a_finished_batch_does_not_claim_eta_is_still_being_estimated(capsys):
    output = StderrTomorrowTrainingProgress(monotonic=_Clock(0, 0))
    output.publish(TomorrowTrainingProgress(
        "model_fit", "running", 0, 83,
        fit_detail=TrainingFitDetail("银行", 20000, "training", 6, 10, 100, 100, industry_name="银行"),
    ))
    text = capsys.readouterr().err
    assert "本轮批次 100/100 (100.0%)" in text
    assert "本轮估算中" not in text and "本轮余约" not in text


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
        "00:00:00 | Tomorrow训练·资源预检 00:00:00 | 完成 | 任务 1/1",
        "00:12:34 | Tomorrow训练·样本构建 00:00:00 | 处理中 | 扫描 850行；生成 612样本行",
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
        "00:00:00 | Tomorrow训练·样本构建 00:00:00 | 开始 | 扫描 0行；生成 0样本行",
        "00:00:10 | Tomorrow训练·样本构建 00:00:10 | 处理中 | 扫描 1行；生成 0样本行",
        "00:00:31 | Tomorrow训练·样本构建 00:00:31 | 处理中 | 扫描 25行；生成 0样本行",
        "00:00:32 | Tomorrow训练·样本构建 00:00:32 | 完成 | 扫描 100行；生成 0样本行",
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
        "00:01:01 | Tomorrow训练·结果汇总 00:00:00 | 阻塞 | 状态 blocked；错误 history_maintenance_running",
        "00:01:02 | Tomorrow训练·结果汇总 00:00:01 | 取消",
    ]


def test_expensive_index_stage_emits_heartbeat_at_thirty_seconds(capsys) -> None:
    output = StderrTomorrowTrainingProgress(monotonic=_Clock(0.0, 0.0, 61.0), start_heartbeat=False)
    output.publish(TomorrowTrainingProgress("sample_index", "started", 0, 1))
    capsys.readouterr()
    output._emit_heartbeat(29.0)
    assert _compact(capsys.readouterr().err) == ""
    output._emit_heartbeat(30.0)
    output._emit_heartbeat(60.0)
    assert _compact(capsys.readouterr().err).splitlines() == [
        "00:00:30 | Tomorrow训练·样本索引 00:00:30 | 等待 | 任务 0/1；未推进 00:00:30",
        "00:01:00 | Tomorrow训练·样本索引 00:01:00 | 等待 | 任务 0/1；未推进 00:01:00",
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


def test_fit_detail_retains_latest_batch_and_reports_each_skip_immediately(capsys) -> None:
    clock = _MutableClock()
    output = StderrTomorrowTrainingProgress(monotonic=clock, start_heartbeat=False)
    output.publish(TomorrowTrainingProgress("model_fit", "started", 0, 83))
    detail = TrainingFitDetail("银行", 20000, "training", 2, 100, 0, 79, industry_name="银行")
    update = TomorrowTrainingProgress("model_fit", "running", 11, 83, fit_detail=detail)
    clock.now = 5
    output.publish(update)
    initial = _compact(capsys.readouterr().err)
    assert "银行·训练" in initial and "轮次 2/100" in initial and "本轮批次 0/79" in initial
    for batch in (1, 2):
        clock.now += 0.1
        output.publish(replace(update, fit_detail=replace(detail, completed_batches=batch)))
    assert _compact(capsys.readouterr().err) == ""
    clock.now = 35
    output._emit_heartbeat(clock.now)
    heartbeat = _compact(capsys.readouterr().err)
    assert "拟合 00:00:35 | 等待" in heartbeat and "本轮批次 2/79" in heartbeat
    skipped = TrainingFitDetail(
        "银行", 20000, "skipped", failure_reason="calibration_prediction_variance_insufficient", industry_name="银行",
    )
    output.publish(replace(update, completed_units=12, fit_detail=skipped))
    assert "calibration_prediction_variance_insufficient" in "".join(capsys.readouterr().err.split())
    output.publish(replace(update, completed_units=12, fit_detail=skipped))
    assert capsys.readouterr().err == ""
    output.publish(replace(update, completed_units=12, fit_detail=TrainingFitDetail(
        "软件", 30000, "preparation", industry_name="软件",
    )))
    assert capsys.readouterr().err == ""
    clock.now = 65
    output._emit_heartbeat(clock.now)
    assert "软件·检查" in _compact(capsys.readouterr().err)
    output.publish_result("engineering_ready", None)
    summary = _compact(capsys.readouterr().err)
    assert "跳过 1项" in summary and "calibration_prediction_variance_insufficient 1项" in "".join(line.strip() for line in summary.splitlines())


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
    assert all(line.startswith("00:00:00 | ") and "生成 0样本行" in line and len(line.split(" | ")) == 4 for line in lines)
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
    output = StderrTomorrowTrainingProgress(monotonic=_Clock(0, 1, 32), start_heartbeat=False, command_label="train")
    output.report_notice("配置检查", "读取本地配置", state="开始")
    output._emit_heartbeat(31)
    assert "00:00:31 | train·配置检查 00:00:30 | 等待 | 读取本地配置" in _compact(capsys.readouterr().err)
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
                    clock[0] = 31.0
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
