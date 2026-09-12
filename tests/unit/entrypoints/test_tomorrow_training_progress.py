from __future__ import annotations

from trader.application.research.tomorrow_training import TomorrowTrainingProgress
from trader.entrypoints.tomorrow_training_progress import StderrTomorrowTrainingProgress


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
        "00:00:00 | 资源预检 | 1/1 (100.00%) | 完成 | 计算线程 3 | 峰值 RSS 目标 4096 MiB",
        "00:12:34 | 历史行转换 | 850/5453 (15.59%) | 运行中 | 已生成样本 612",
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
        "00:00:00 | 输入分片校验 | 0/100 (0.00%) | 开始",
        "00:00:31 | 输入分片校验 | 25/100 (25.00%) | 运行中",
        "00:00:32 | 输入分片校验 | 100/100 (100.00%) | 完成",
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
