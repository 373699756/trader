from __future__ import annotations

from dataclasses import replace

import pytest

from trader.training.application.tomorrow_training import TomorrowTrainingProgress, TrainingFitDetail
from trader.training.entrypoints.tomorrow_training_progress import StderrTomorrowTrainingProgress


class _Clock:
    def __init__(self, *values: float) -> None:
        self._values = iter(values)

    def __call__(self) -> float:
        return next(self._values)


def test_training_progress_prints_real_rows_percent_duration_and_no_rss(capsys) -> None:
    output = StderrTomorrowTrainingProgress(monotonic=_Clock(100.0, 100.0, 854.0), start_heartbeat=False)

    output.publish(TomorrowTrainingProgress("resource_preflight", "completed", 1, 1))
    output.publish(TomorrowTrainingProgress("history_conversion", "running", 850, 5453, produced_units=612))

    lines = capsys.readouterr().err.splitlines()
    assert lines == [
        "00:00:00 | Tomorrow训练 | 资源预检 | 1/1 (100.00%) | 完成 | 阶段耗时 00:00:00 "
        "| 计算线程 2 | 峰值 RSS 上限 2048 MiB",
        "00:12:34 | Tomorrow训练 | 历史读取与训练样本生成 | 850/5453 (15.59%) | 运行中 "
        "| 阶段耗时 00:00:00 | 已生成样本 612",
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

    assert capsys.readouterr().err.splitlines() == [
        "00:00:00 | Tomorrow训练 | 历史读取与训练样本生成 | 0/100 (0.00%) | 开始 | 阶段耗时 00:00:00 | 已生成样本 0",
        "00:00:10 | Tomorrow训练 | 历史读取与训练样本生成 | 1/100 (1.00%) | 运行中 | 阶段耗时 00:00:10 | 已生成样本 0",
        "00:00:31 | Tomorrow训练 | 历史读取与训练样本生成 | 25/100 (25.00%) | 运行中 "
        "| 阶段耗时 00:00:31 | 已生成样本 0",
        "00:00:32 | Tomorrow训练 | 历史读取与训练样本生成 | 100/100 (100.00%) | 完成 "
        "| 阶段耗时 00:00:32 | 已生成样本 0",
    ]
    assert output.stage_durations == (("history_conversion", 32.0),)


def test_training_progress_prints_compact_blocked_and_cancelled_results(capsys) -> None:
    output = StderrTomorrowTrainingProgress(
        monotonic=_Clock(100.0, 161.0, 162.0),
        start_heartbeat=False,
    )

    output.publish_result("blocked", "history_maintenance_running")
    output.publish_cancelled()

    assert capsys.readouterr().err.splitlines() == [
        "00:01:01 | Tomorrow训练 | 0/1 (0.00%) | 阻塞 | 错误 history_maintenance_running",
        "00:01:02 | Tomorrow训练 | 0/1 (0.00%) | 已取消",
    ]


def test_expensive_index_stage_emits_heartbeat_at_ten_seconds(capsys) -> None:
    output = StderrTomorrowTrainingProgress(monotonic=_Clock(0.0, 0.0, 61.0), start_heartbeat=False)
    output.publish(TomorrowTrainingProgress("sample_index", "started", 0, 1))
    capsys.readouterr()
    output._emit_heartbeat(9.0)
    assert capsys.readouterr().err == ""
    output._emit_heartbeat(10.0)
    output._emit_heartbeat(20.0)
    assert capsys.readouterr().err.splitlines() == [
        "00:00:10 | Tomorrow训练 | 样本索引 | 0/1 (0.00%) | 运行中 | 阶段耗时 00:00:10",
        "00:00:20 | Tomorrow训练 | 样本索引 | 0/1 (0.00%) | 运行中 | 阶段耗时 00:00:20",
    ]
    output.publish_result("engineering_ready", None)
    capsys.readouterr()
    output._emit_heartbeat(120.0)
    assert capsys.readouterr().err == ""


def test_repeated_head_stages_reset_elapsed_and_sum_evidence(capsys) -> None:
    from trader.recommendation.domain.publication.models import Strategy

    output = StderrTomorrowTrainingProgress(monotonic=_Clock(0, 0, 10, 20, 25, 40, 42), start_heartbeat=False)
    for strategy in (Strategy.TOMORROW, Strategy.D25, Strategy.TOMORROW):
        output.publish(TomorrowTrainingProgress("model_fit", "started", 0, 1, strategy=strategy))
        output.publish(TomorrowTrainingProgress("model_fit", "completed", 1, 1, strategy=strategy))
    assert output.stage_durations == (("model_fit", 17.0),)
    assert "阶段耗时 00:00:02" in capsys.readouterr().err.splitlines()[-1]


def test_duplicate_completion_does_not_double_count_stage_duration() -> None:
    output = StderrTomorrowTrainingProgress(monotonic=_Clock(0, 0, 10, 15, 20), start_heartbeat=False)
    output.publish(TomorrowTrainingProgress("sample_index", "started", 0, 1))
    output.publish(TomorrowTrainingProgress("sample_index", "completed", 1, 1))
    output.publish(TomorrowTrainingProgress("target_statistics", "started", 0, 1))
    output.publish(TomorrowTrainingProgress("sample_index", "completed", 1, 1))
    assert output.stage_durations == (("sample_index", 10.0),)


def test_fit_detail_retains_latest_batch_without_flooding_and_emits_skips_immediately(capsys) -> None:
    output = StderrTomorrowTrainingProgress(monotonic=_Clock(0, 0, 1, 2, 3, 11, 12), start_heartbeat=False)
    output.publish(TomorrowTrainingProgress("model_fit", "started", 0, 83))
    detail = TrainingFitDetail("银行", 20000, "training", 2, 100, 0, 79)
    update = TomorrowTrainingProgress("model_fit", "running", 11, 83, fit_detail=detail)
    output.publish(update)
    assert "行业 银行 | 训练样本 20000 | 训练 | 轮次 2/100 | 已完成批次 0/79" in capsys.readouterr().err
    for batch in (1, 2):
        output.publish(replace(update, fit_detail=replace(detail, completed_batches=batch)))
    assert capsys.readouterr().err == ""
    output._emit_heartbeat(11)
    heartbeat = capsys.readouterr().err
    assert "阶段耗时 00:00:11" in heartbeat and "已完成批次 2/79" in heartbeat
    skipped = TrainingFitDetail("银行", 20000, "skipped", failure_reason="calibration_prediction_variance_insufficient")
    output.publish(replace(update, completed_units=12, fit_detail=skipped))
    assert "行业跳过 | 原因 calibration_prediction_variance_insufficient" in capsys.readouterr().err
    output.publish(replace(update, completed_units=12, fit_detail=TrainingFitDetail("软件", 30000, "preparation")))
    assert "行业 软件 | 训练样本 30000 | 样本检查" in capsys.readouterr().err


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
