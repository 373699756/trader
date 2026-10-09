"""Compact stderr progress for the explicit Tomorrow training command."""

from __future__ import annotations

import sys
import threading
import time
from collections.abc import Callable
from types import TracebackType
from typing import Literal

from trader.recommendation.domain.publication.models import Strategy
from trader.training.application.tomorrow_training import (
    TOMORROW_TRAINING_COMPUTE_THREADS,
    TOMORROW_TRAINING_PEAK_RSS_MIB,
    TomorrowTrainingProgress,
    TomorrowTrainingStage,
)

_STAGE_LABELS: dict[TomorrowTrainingStage, str] = {
    "resource_preflight": "资源预检",
    "input_read": "读取训练输入",
    "training_preparation": "训练准备",
    "partition_validation": "输入分片校验",
    "history_conversion": "历史行转换",
    "cross_section_conversion": "横截面转换",
    "sample_index": "样本索引",
    "target_statistics": "标签统计",
    "industry_statistics": "行业统计",
    "model_fit": "行业模型",
    "artifact_publish": "工件发布",
}
_STATE_LABELS = {"started": "开始", "running": "运行中", "completed": "完成"}
_PROGRESS_INTERVAL_SECONDS = 30.0
_HEARTBEAT_INTERVAL_SECONDS = 30.0
TomorrowTrainingCommandStatus = Literal["blocked", "rejected", "engineering_ready"]
_RESULT_LABELS: dict[TomorrowTrainingCommandStatus, str] = {
    "blocked": "阻塞",
    "rejected": "拒绝",
    "engineering_ready": "完成",
}
_PARTITION_PHASE_LABELS = {"hash": "SHA-256", "integrity": "完整性", "row_count": "行数"}


class StderrTomorrowTrainingProgress:
    def __init__(
        self,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        start_heartbeat: bool = True,
        command_label: str = "Tomorrow训练",
    ) -> None:
        self._monotonic = monotonic
        self._started_at = monotonic()
        self._start_heartbeat = start_heartbeat
        self._command_label = command_label
        self._last_emitted_at: float | None = None
        self._latest: TomorrowTrainingProgress | None = None
        self._stage_started_at: dict[tuple[TomorrowTrainingStage, Strategy | None], float] = {}
        self._completed_stages: set[tuple[TomorrowTrainingStage, Strategy | None]] = set()
        self._stage_durations: dict[TomorrowTrainingStage, float] = {}
        self._lock = threading.Lock()
        self._stopped = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> StderrTomorrowTrainingProgress:
        if self._start_heartbeat:
            self._thread = threading.Thread(
                target=self._heartbeat_loop,
                name="tomorrow-training-progress",
                daemon=True,
            )
            self._thread.start()
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc_value: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        self._stopped.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def publish(self, progress: TomorrowTrainingProgress) -> None:
        now = self._monotonic()
        with self._lock:
            key = (progress.stage, progress.strategy)
            if progress.state == "started" or key not in self._stage_started_at:
                self._stage_started_at[key] = now
                self._completed_stages.discard(key)
            if progress.state == "completed" and key not in self._completed_stages:
                duration = now - self._stage_started_at[key]
                self._stage_durations[progress.stage] = self._stage_durations.get(progress.stage, 0.0) + duration
                self._completed_stages.add(key)
            self._latest = progress
            boundary = progress.state in {"started", "completed"}
            due = self._last_emitted_at is None or now - self._last_emitted_at >= _PROGRESS_INTERVAL_SECONDS
            if boundary or due:
                self._emit(progress, now)

    @property
    def stage_durations(self) -> tuple[tuple[TomorrowTrainingStage, float], ...]:
        with self._lock:
            return tuple(self._stage_durations.items())

    def publish_cancelled(self) -> None:
        now = self._monotonic()
        with self._lock:
            self._latest = None
            print(
                f"{_format_duration(now - self._started_at)} | {self._command_label} | 0/1 (0.00%) | 已取消",
                file=sys.stderr,
                flush=True,
            )
            self._last_emitted_at = now

    def publish_result(self, status: TomorrowTrainingCommandStatus, failure_reason: str | None) -> None:
        now = self._monotonic()
        completed = 1 if status == "engineering_ready" else 0
        parts = [
            _format_duration(now - self._started_at),
            self._command_label,
            f"{completed}/1 ({completed * 100:.2f}%)",
            _RESULT_LABELS[status],
        ]
        if failure_reason is not None:
            parts.append(f"错误 {failure_reason}")
        with self._lock:
            self._latest = None
            print(" | ".join(parts), file=sys.stderr, flush=True)
            self._last_emitted_at = now

    def _heartbeat_loop(self) -> None:
        while not self._stopped.wait(1.0):
            self._emit_heartbeat(self._monotonic())

    def _emit_heartbeat(self, now: float) -> None:
        with self._lock:
            if (
                self._latest is not None
                and self._latest.state != "completed"
                and self._last_emitted_at is not None
                and now - self._last_emitted_at >= _HEARTBEAT_INTERVAL_SECONDS
            ):
                self._emit(self._latest, now)

    def _emit(self, progress: TomorrowTrainingProgress, now: float) -> None:
        percent = 100.0 if progress.total_units == 0 else progress.completed_units / progress.total_units * 100.0
        parts = [
            _format_duration(now - self._started_at),
            (
                f"{progress.strategy.value} {_STAGE_LABELS[progress.stage]}"
                if progress.strategy is not None
                else _STAGE_LABELS[progress.stage]
            ),
            f"{progress.completed_units}/{progress.total_units} ({percent:.2f}%)",
            _STATE_LABELS[progress.state],
            f"阶段耗时 {_format_duration(now - self._stage_started_at[(progress.stage, progress.strategy)])}",
        ]
        if progress.stage == "resource_preflight":
            parts.extend(
                (
                    f"计算线程 {TOMORROW_TRAINING_COMPUTE_THREADS}",
                    f"峰值 RSS 上限 {TOMORROW_TRAINING_PEAK_RSS_MIB} MiB",
                )
            )
        elif progress.stage == "partition_validation" and progress.partition_validation is not None:
            detail = progress.partition_validation
            completed_mib = detail.completed_bytes / (1024 * 1024)
            total_mib = detail.total_bytes / (1024 * 1024)
            parts.append(
                f"当前分片 {detail.current_partition}/{detail.total_partitions}"
                f" {_PARTITION_PHASE_LABELS[detail.phase]} {completed_mib:.1f}/{total_mib:.1f} MiB"
            )
        elif progress.stage == "history_conversion":
            parts.append(f"已生成样本 {progress.produced_units}")
        elif progress.stage == "cross_section_conversion":
            parts.append(f"有效样本 {progress.produced_units}")
        elif progress.stage == "model_fit":
            parts.append(f"有效模型 {progress.produced_units}")
        print(" | ".join(parts), file=sys.stderr, flush=True)
        self._last_emitted_at = now


def _format_duration(seconds: float) -> str:
    total_seconds = int(max(0.0, seconds))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, remaining_seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{remaining_seconds:02d}"


__all__ = ["StderrTomorrowTrainingProgress"]
