from __future__ import annotations

from trader.training.application.tomorrow_training import (
    TomorrowTrainingPartitionValidationProgress,
    TomorrowTrainingProgress,
)
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
        "00:00:00 | 资源预检 | 1/1 (100.00%) | 完成 | 阶段耗时 00:00:00 | 计算线程 2 | 峰值 RSS 上限 2048 MiB",
        "00:12:34 | 历史行转换 | 850/5453 (15.59%) | 运行中 | 阶段耗时 00:00:00 | 已生成样本 612",
    ]


def test_training_progress_throttles_advances_but_emits_stage_boundaries(capsys) -> None:
    output = StderrTomorrowTrainingProgress(
        monotonic=_Clock(100.0, 100.0, 110.0, 131.0, 132.0),
        start_heartbeat=False,
    )

    output.publish(TomorrowTrainingProgress("partition_validation", "started", 0, 100))
    output.publish(TomorrowTrainingProgress("partition_validation", "running", 1, 100))
    output.publish(TomorrowTrainingProgress("partition_validation", "running", 25, 100))
    output.publish(TomorrowTrainingProgress("partition_validation", "completed", 100, 100))

    assert capsys.readouterr().err.splitlines() == [
        "00:00:00 | 输入分片校验 | 0/100 (0.00%) | 开始 | 阶段耗时 00:00:00",
        "00:00:31 | 输入分片校验 | 25/100 (25.00%) | 运行中 | 阶段耗时 00:00:31",
        "00:00:32 | 输入分片校验 | 100/100 (100.00%) | 完成 | 阶段耗时 00:00:32",
    ]
    assert output.stage_durations == (("partition_validation", 32.0),)


def test_training_progress_shows_current_partition_hash_bytes(capsys) -> None:
    output = StderrTomorrowTrainingProgress(monotonic=_Clock(100.0, 100.0), start_heartbeat=False)

    output.publish(
        TomorrowTrainingProgress(
            "partition_validation",
            "running",
            0,
            100,
            partition_validation=TomorrowTrainingPartitionValidationProgress(
                1, 100, 128 * 1024**2, 512 * 1024**2, "hash"
            ),
        )
    )

    assert capsys.readouterr().err.splitlines() == [
        "00:00:00 | 输入分片校验 | 0/100 (0.00%) | 运行中 | 阶段耗时 00:00:00 | 当前分片 1/100 SHA-256 128.0/512.0 MiB"
    ]


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


def test_expensive_index_stage_emits_heartbeat_at_thirty_seconds(capsys) -> None:
    output = StderrTomorrowTrainingProgress(monotonic=_Clock(0.0, 0.0, 61.0), start_heartbeat=False)
    output.publish(TomorrowTrainingProgress("sample_index", "started", 0, 1))
    capsys.readouterr()
    output._emit_heartbeat(29.0)
    assert capsys.readouterr().err == ""
    output._emit_heartbeat(30.0)
    output._emit_heartbeat(60.0)
    assert capsys.readouterr().err.splitlines() == [
        "00:00:30 | 样本索引 | 0/1 (0.00%) | 开始 | 阶段耗时 00:00:30",
        "00:01:00 | 样本索引 | 0/1 (0.00%) | 开始 | 阶段耗时 00:01:00",
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
