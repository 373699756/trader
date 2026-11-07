"""Compact stderr progress for the explicit Tomorrow training command."""

from __future__ import annotations

import sys
import math
import threading
import time
from collections.abc import Callable
from dataclasses import replace
from types import TracebackType
from typing import Literal

from trader.infra.progress_format import CleanProgressText, ProgressDuration, ProgressPrefix
from trader.infra.progress_heartbeat import ProgressHeartbeat
from trader.infra.shutdown import ShutdownStep
from trader.infra.signal_cancellation import ManualCancellationScope
from trader.recommendation.domain.publication.models import Strategy
from trader.training.application.tomorrow_training import (
    TOMORROW_TRAINING_PEAK_RSS_MIB,
    TomorrowTrainingProgress,
    TomorrowTrainingStage,
    TrainingFitPhase,
)

_STAGE_LABELS: dict[TomorrowTrainingStage, str] = {
    "resource_preflight": "资源预检",
    "input_read": "读取训练输入",
    "training_preparation": "训练准备",
    "history_conversion": "历史读取与训练样本生成",
    "cross_section_conversion": "横截面转换",
    "sample_index": "样本索引",
    "sample_cache": "完整样本缓存",
    "sample_cache_check": "样本缓存完整性校验",
    "sample_cache_seal": "样本缓存封存",
    "target_statistics": "标签统计",
    "industry_statistics": "行业统计",
    "model_fit": "行业模型",
    "final_model_refit": "最新成熟数据重拟合",
    "artifact_publish": "工件发布",
}
_STATE_LABELS = {"started": "开始", "running": "处理中", "completed": "完成"}
_FIT_PHASE_LABELS: dict[TrainingFitPhase, str] = {
    "preparation": "样本检查",
    "batch_cache": "行业样本缓存",
    "normalization": "标准化统计",
    "training": "训练",
    "early_stopping": "早停验证",
    "calibration": "独立校准",
    "completed": "行业拟合完成",
    "skipped": "行业跳过",
}
_PROGRESS_INTERVAL_SECONDS = 1.0
_HEARTBEAT_INTERVAL_SECONDS = 5.0
TomorrowTrainingCommandStatus = Literal["blocked", "rejected", "engineering_ready", "failed"]
_RESULT_LABELS: dict[TomorrowTrainingCommandStatus, str] = {
    "blocked": "阻塞",
    "rejected": "拒绝",
    "engineering_ready": "完成",
    "failed": "失败",
}


class StderrTomorrowTrainingProgress:
    def __init__(
        self,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        start_heartbeat: bool = False,
        command_label: str = "Tomorrow训练",
        started_at: float | None = None,
        cancellation: ManualCancellationScope | None = None,
    ) -> None:
        self._monotonic = monotonic
        now = monotonic()
        self._started_at = now if started_at is None else started_at
        if not math.isfinite(self._started_at) or not 0 <= self._started_at <= now:
            raise ValueError("training_progress_start_invalid")
        self._start_heartbeat = start_heartbeat
        if start_heartbeat and cancellation is None:
            raise ValueError("training_progress_shutdown_scope_required")
        self._cancellation = cancellation
        self._shutdown_step: ShutdownStep | None = None
        self._command_label = command_label
        self._latest: TomorrowTrainingProgress | None = None
        self._stage_started_at: dict[tuple[TomorrowTrainingStage, Strategy | None], float] = {}
        self._completed_stages: set[tuple[TomorrowTrainingStage, Strategy | None]] = set()
        self._stage_durations: dict[TomorrowTrainingStage, float] = {}
        self._display_started: dict[tuple[TomorrowTrainingStage, Strategy | None], float] = {}
        self._latest_by_stage: dict[tuple[TomorrowTrainingStage, Strategy | None], TomorrowTrainingProgress] = {}
        self._advanced_at: dict[tuple[TomorrowTrainingStage, Strategy | None], float] = {}
        self._emitted_at: dict[tuple[TomorrowTrainingStage, Strategy | None], float] = {}
        self._notice_started: dict[str, float] = {}
        self._notice_active: dict[str, tuple[str, float]] = {}
        self._notice_emitted: dict[str, float] = {}
        self._lock = threading.Lock()
        self._heartbeat = ProgressHeartbeat[bool](lambda _tick: self._emit_heartbeat(self._monotonic()), interval_seconds=1)

    def __enter__(self) -> StderrTomorrowTrainingProgress:
        if self._start_heartbeat:
            self._heartbeat.__enter__()
            self._heartbeat(True)
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc_value: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        if self._start_heartbeat:
            if self._cancellation is None:
                raise RuntimeError("training_progress_shutdown_scope_missing")
            self._shutdown_step = self._heartbeat.stop(self._cancellation.shutdown_deadline())
            self._cancellation.record_shutdown(self._shutdown_step)
            if _exc_type is None:
                if self._heartbeat.failure is not None:
                    raise self._heartbeat.failure
                if not self._shutdown_step.completed:
                    raise TimeoutError("training_progress_shutdown_incomplete")

    @property
    def shutdown_step(self) -> ShutdownStep | None:
        return self._shutdown_step

    @property
    def started_at(self) -> float:
        return self._started_at

    def report_notice(self, stage: str, details: str, *, state: str = "处理中") -> None:
        now = self._monotonic()
        with self._lock:
            started = self._notice_started.setdefault(stage, now)
            prefix = ProgressPrefix(now - self._started_at, f"{self._command_label}·{stage}", now - started, state)
            _write_line(" | ".join((*prefix, CleanProgressText(details))))
            self._notice_emitted[stage] = now
            if state in {"完成", "失败", "取消"}:
                self._notice_active.pop(stage, None)
            else:
                self._notice_active[stage] = (details, now)

    def report_disk(self, text: str) -> None:
        self.report_notice("存储检测", text)

    def publish(self, progress: TomorrowTrainingProgress) -> None:
        now = self._monotonic()
        with self._lock:
            key = (progress.stage, progress.strategy)
            self._display_started.setdefault(key, now)
            previous_progress = self._latest_by_stage.get(key)
            if previous_progress != progress:
                self._advanced_at[key] = now
            self._latest_by_stage[key] = progress
            if progress.state == "started" or key not in self._stage_started_at:
                self._stage_started_at[key] = now
                self._completed_stages.discard(key)
            if progress.state == "completed" and key not in self._completed_stages:
                duration = now - self._stage_started_at[key]
                self._stage_durations[progress.stage] = self._stage_durations.get(progress.stage, 0.0) + duration
                self._completed_stages.add(key)
            boundary = progress.state in {"started", "completed"}
            if progress.fit_detail is not None:
                previous = self._latest.fit_detail if self._latest is not None else None
                boundary = boundary or (
                    previous is None
                    or previous.industry != progress.fit_detail.industry
                    or (progress.fit_detail.phase in {"skipped", "completed"} and previous != progress.fit_detail)
                )
            self._latest = progress
            due = key not in self._emitted_at or now - self._emitted_at[key] >= _PROGRESS_INTERVAL_SECONDS
            if boundary or due:
                self._emit(progress, now)

    @property
    def stage_durations(self) -> tuple[tuple[TomorrowTrainingStage, float], ...]:
        with self._lock:
            return tuple(self._stage_durations.items())

    def publish_cancelled(self) -> None:
        now = self._monotonic()
        with self._lock:
            latest = self._latest
            self._latest = None
            self._latest_by_stage.clear()
            self._notice_active.clear()
            stage_started = self._notice_started.setdefault("结果汇总", now)
            parts = [*ProgressPrefix(now - self._started_at, f"{self._command_label}·结果汇总", now - stage_started, "取消")]
            if latest is not None:
                parts.append(f"最后阶段 {_STAGE_LABELS[latest.stage]} {latest.completed_units}/{latest.total_units}")
            _write_line(" | ".join(parts))

    def publish_result(self, status: TomorrowTrainingCommandStatus, failure_reason: str | None) -> None:
        now = self._monotonic()
        with self._lock:
            stage_started = self._notice_started.setdefault("结果汇总", now)
            parts = [
                *ProgressPrefix(now - self._started_at, f"{self._command_label}·结果汇总", now - stage_started, _RESULT_LABELS[status]),
                f"状态 {status}",
            ]
            if failure_reason is not None:
                parts.append(f"错误 {CleanProgressText(failure_reason)}")
            self._latest = None
            self._latest_by_stage.clear()
            self._notice_active.clear()
            _write_line(" | ".join(parts))

    def _emit_heartbeat(self, now: float) -> None:
        with self._lock:
            for stage, (details, updated) in self._notice_active.items():
                if now - self._notice_emitted[stage] >= _HEARTBEAT_INTERVAL_SECONDS:
                    prefix = ProgressPrefix(now - self._started_at, f"{self._command_label}·{stage}", now - self._notice_started[stage], "等待")
                    _write_line(" | ".join((*prefix, CleanProgressText(details), f"距上次阶段更新 {ProgressDuration(now - updated)}")))
                    self._notice_emitted[stage] = now
            for key, latest in self._latest_by_stage.items():
                if latest.state != "completed" and now - self._emitted_at[key] >= _HEARTBEAT_INTERVAL_SECONDS:
                    self._emit(replace(latest, state="running"), now, waiting=True)

    def _emit(self, progress: TomorrowTrainingProgress, now: float, *, waiting: bool = False) -> None:
        percent = 100.0 if progress.total_units == 0 else progress.completed_units / progress.total_units * 100.0
        key = (progress.stage, progress.strategy)
        label = f"{progress.strategy.value}·{_STAGE_LABELS[progress.stage]}" if progress.strategy is not None else _STAGE_LABELS[progress.stage]
        parts = [
            *ProgressPrefix(now - self._started_at, f"{self._command_label}·{label}", now - self._display_started[key], "等待" if waiting else _STATE_LABELS[progress.state]),
            f"{progress.completed_units}/{progress.total_units} ({percent:.2f}%)",
        ]
        if waiting:
            parts.append(f"距上次进度更新 {ProgressDuration(now - self._advanced_at[key])}")
        if progress.stage == "resource_preflight":
            parts.extend(
                (
                    f"峰值 RSS 上限 {TOMORROW_TRAINING_PEAK_RSS_MIB} MiB",
                )
            )
        elif progress.stage == "sample_cache" and progress.state == "completed":
            parts.append("复用已校验完整样本" if progress.produced_units else "需要构建样本")
        elif progress.stage in {"sample_cache_check", "sample_cache_seal"}:
            parts[3] = (
                f"{progress.completed_units / 1024**2:.1f}/{progress.total_units / 1024**2:.1f} MiB ({percent:.2f}%)"
            )
        elif progress.stage == "history_conversion":
            parts.append(f"已生成样本 {progress.produced_units}")
        elif progress.stage == "cross_section_conversion":
            parts.append(f"有效样本 {progress.produced_units}")
        elif progress.stage in {"model_fit", "final_model_refit"}:
            parts.append(f"有效模型 {progress.produced_units}")
        if progress.fit_detail is not None:
            detail = progress.fit_detail
            parts.extend(
                (f"行业 {CleanProgressText(detail.industry)}", f"训练样本 {detail.training_rows}", _FIT_PHASE_LABELS[detail.phase])
            )
            if detail.epoch:
                parts.append(f"轮次 {detail.epoch}/{detail.total_epochs}")
            if detail.total_batches:
                parts.append(f"已完成批次 {detail.completed_batches}/{detail.total_batches}")
            if detail.failure_reason:
                parts.append(f"原因 {CleanProgressText(detail.failure_reason)}")
        if progress.sample_diagnostics is not None:
            diagnostic = progress.sample_diagnostics
            parts.append(
                f"缺行业 {diagnostic.missing_industry} | 缺价格 {diagnostic.missing_prices} "
                f"| 窗口不足 {diagnostic.short_window} "
                f"| 特征无效 {diagnostic.invalid_features} | 标签不足 {diagnostic.missing_labels}"
                f" | 停牌日 {diagnostic.suspended_sessions} | 缺行估算日 {diagnostic.estimated_sessions}"
                f" | T+1标签缺失 {diagnostic.missing_t1} | D25标签缺失 {diagnostic.missing_d25}"
            )
        _write_line(" | ".join(parts))
        self._emitted_at[key] = now


def _write_line(line: str) -> None:
    # Keep the newline in the same stream write across independent profile emitters.
    sys.stderr.write(line.rstrip(" ") + "\n")
    sys.stderr.flush()


__all__ = ["StderrTomorrowTrainingProgress"]
