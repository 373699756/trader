"""Compact stderr progress for the explicit Tomorrow training command."""

from __future__ import annotations

import sys
import math
import os
import threading
import time
from collections import Counter, deque
from collections.abc import Callable
from dataclasses import dataclass
from types import TracebackType
from typing import Literal

from trader.infra.progress_format import CleanProgressText, DisplayWidth, ProgressDuration, ProgressPrefix, WrapProgressText
from trader.infra.progress_heartbeat import ProgressHeartbeat
from trader.infra.shutdown import ShutdownStep
from trader.infra.signal_cancellation import ManualCancellationScope
from trader.recommendation.domain.publication.models import Strategy
from trader.training.application.tomorrow_training import (
    TomorrowTrainingProgress,
    TomorrowTrainingStage,
    TrainingFitPhase,
    TrainingFitDetail,
    TrainingSampleDiagnostics,
)

_STAGE_LABELS: dict[TomorrowTrainingStage, str] = {
    "resource_preflight": "资源预检",
    "input_read": "输入检查",
    "training_preparation": "训练准备",
    "history_conversion": "样本构建",
    "cross_section_conversion": "生成样本",
    "sample_index": "样本索引",
    "sample_cache": "样本缓存",
    "sample_cache_check": "缓存检查",
    "sample_cache_seal": "缓存封存",
    "target_statistics": "标签统计",
    "industry_statistics": "行业统计",
    "model_fit": "拟合",
    "final_model_refit": "重拟合",
    "artifact_publish": "模型发布",
}
_STATE_LABELS = {"started": "开始", "running": "处理中", "completed": "完成"}
_FIT_PHASE_LABELS: dict[TrainingFitPhase, str] = {
    "preparation": "检查", "batch_cache": "缓存", "normalization": "标准化",
    "training": "训练", "early_stopping": "早停", "calibration": "校准",
    "completed": "完成", "skipped": "跳过",
}
_HEAD_LABELS = {Strategy.TOMORROW: "明日", Strategy.D25: "D25"}
_PROGRESS_INTERVAL_SECONDS = 5.0
_HEARTBEAT_INTERVAL_SECONDS = 5.0
TomorrowTrainingCommandStatus = Literal["blocked", "rejected", "engineering_ready", "failed"]
_RESULT_LABELS: dict[TomorrowTrainingCommandStatus, str] = {
    "blocked": "阻塞",
    "rejected": "拒绝",
    "engineering_ready": "完成",
    "failed": "失败",
}


@dataclass(frozen=True)
class TrainingProgressLayout:
    columns: int = 180

    def __post_init__(self) -> None:
        if type(self.columns) is not int or self.columns < 1:
            raise ValueError("training_progress_columns_invalid")

    def render(self, prefix: tuple[str, str, str], details: tuple[str, ...]) -> str:
        first = " | ".join(prefix)
        text = "；".join(CleanProgressText(part).strip() for part in details if part.strip())
        if not text:
            return first.rstrip()
        offset = DisplayWidth(first) + 3
        wrapped = WrapProgressText(text, max(2, self.columns - offset))
        return "\n".join((first + " | " + wrapped[0], *(" " * offset + line for line in wrapped[1:])))


def ReadTrainingProgressLayout() -> TrainingProgressLayout:
    """Detect the actual stderr terminal once at the command boundary."""
    try:
        columns = os.get_terminal_size(sys.stderr.fileno()).columns
    except (AttributeError, OSError, ValueError):
        columns = 180
    return TrainingProgressLayout(columns if columns > 0 else 180)


class StderrTomorrowTrainingProgress:
    def __init__(
        self,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        start_heartbeat: bool = False,
        command_label: str = "Tomorrow训练",
        started_at: float | None = None,
        cancellation: ManualCancellationScope | None = None,
        layout: TrainingProgressLayout | None = None,
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
        self._layout = TrainingProgressLayout() if layout is None else layout
        self._latest: TomorrowTrainingProgress | None = None
        self._stage_started_at: dict[tuple[TomorrowTrainingStage, Strategy | None], float] = {}
        self._completed_stages: set[tuple[TomorrowTrainingStage, Strategy | None]] = set()
        self._stage_durations: dict[TomorrowTrainingStage, float] = {}
        self._display_started: dict[tuple[str, Strategy | None], float] = {}
        self._advanced_at = now
        self._emitted_at = float("-inf")
        self._notice_started: dict[str, float] = {}
        self._current_notice: tuple[str, str, float] | None = None
        self._eta_key: tuple[TomorrowTrainingStage, Strategy | None, str, TrainingFitPhase, int] | None = None
        self._eta_samples: deque[tuple[float, int]] = deque(maxlen=32)
        self._skipped: set[tuple[TomorrowTrainingStage, Strategy | None, str]] = set()
        self._skip_reasons: Counter[str] = Counter()
        self._sample_diagnostics: TrainingSampleDiagnostics | None = None
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

    @property
    def layout(self) -> TrainingProgressLayout:
        return self._layout

    def report_notice(self, stage: str, details: str, *, state: str = "处理中") -> None:
        now = self._monotonic()
        with self._lock:
            started = self._notice_started.setdefault(stage, now)
            prefix = ProgressPrefix(now - self._started_at, f"{self._command_label}·{stage}", now - started, state, stage_width=36)
            _write_line(self._layout.render(prefix, (details,)))
            self._emitted_at = now
            self._current_notice = None if state in {"完成", "失败", "取消"} else (stage, details, now)

    def report_disk(self, text: str) -> None:
        self.report_notice("存储检测", text)

    def publish(self, progress: TomorrowTrainingProgress) -> None:
        now = self._monotonic()
        with self._lock:
            key = (progress.stage, progress.strategy)
            if progress.state == "completed" and key in self._completed_stages:
                return
            display_key = _display_key(progress)
            validation_started = progress.validation is not None and (
                display_key not in self._display_started or self._latest is None or self._latest.validation is None
            )
            self._display_started.setdefault(display_key, now)
            if _has_advanced(self._latest, progress):
                self._advanced_at = now
            if progress.state == "started" or key not in self._stage_started_at:
                self._stage_started_at[key] = now
                self._completed_stages.discard(key)
            if progress.state == "completed" and key not in self._completed_stages:
                duration = now - self._stage_started_at[key]
                self._stage_durations[progress.stage] = self._stage_durations.get(progress.stage, 0.0) + duration
                self._completed_stages.add(key)
            if progress.sample_diagnostics is not None:
                self._sample_diagnostics = progress.sample_diagnostics
            detail = progress.fit_detail
            if detail is not None and detail.phase == "skipped":
                skipped = (progress.stage, progress.strategy, detail.industry)
                if skipped not in self._skipped:
                    self._skipped.add(skipped)
                    self._skip_reasons[detail.failure_reason or "unknown"] += 1
            self._latest = progress
            self._current_notice = None
            self._observe_epoch(progress, now)
            boundary = progress.state in {"started", "completed"} or validation_started
            if boundary or now - self._emitted_at >= _PROGRESS_INTERVAL_SECONDS:
                self._emit(progress, now)

    @property
    def stage_durations(self) -> tuple[tuple[TomorrowTrainingStage, float], ...]:
        with self._lock:
            return tuple(self._stage_durations.items())

    def publish_cancelled(self) -> None:
        now = self._monotonic()
        with self._lock:
            latest = self._latest
            details = []
            if latest is not None:
                details.append(f"最后阶段 {self._stage_label(latest)} {self._count(latest)}")
            self._finish(now, "取消", details)

    def publish_result(self, status: TomorrowTrainingCommandStatus, failure_reason: str | None) -> None:
        now = self._monotonic()
        with self._lock:
            details = [f"状态 {status}"]
            if failure_reason is not None:
                details.append(f"错误 {CleanProgressText(failure_reason)}")
            self._finish(now, _RESULT_LABELS[status], details)

    def _finish(self, now: float, state: str, details: list[str]) -> None:
        started = self._notice_started.setdefault("结果汇总", now)
        prefix = ProgressPrefix(now - self._started_at, f"{self._command_label}·结果汇总", now - started, state, stage_width=36)
        if self._skip_reasons:
            details.append(f"跳过 {sum(self._skip_reasons.values())}项")
        details.extend(f"{CleanProgressText(reason)} {count}项" for reason, count in self._skip_reasons.most_common(8))
        if self._stage_durations:
            details.append("累计耗时 " + "，".join(
                f"{_STAGE_LABELS[stage]} {ProgressDuration(seconds)}" for stage, seconds in self._stage_durations.items()
            ))
        diagnostic = self._sample_diagnostics
        if diagnostic is not None:
            details.append(
                f"候选样本诊断（股日） 缺行业{diagnostic.missing_industry} 缺价格{diagnostic.missing_prices} "
                f"窗口不足（含预热）{diagnostic.short_window} 特征无效{diagnostic.invalid_features} 标签不足{diagnostic.missing_labels} "
                f"停牌{diagnostic.suspended_sessions} 估算缺行{diagnostic.estimated_sessions} "
                f"T+1缺失{diagnostic.missing_t1} D25缺失{diagnostic.missing_d25}"
            )
        self._latest = None
        self._current_notice = None
        self._eta_key = None
        self._eta_samples.clear()
        _write_line(self._layout.render(prefix, tuple(details)))

    def _emit_heartbeat(self, now: float) -> None:
        with self._lock:
            if now - self._emitted_at < _HEARTBEAT_INTERVAL_SECONDS:
                return
            notice = self._current_notice
            if notice is not None:
                stage, details, updated = notice
                prefix = ProgressPrefix(now - self._started_at, f"{self._command_label}·{stage}",
                                        now - self._notice_started[stage], "等待", stage_width=36)
                _write_line(self._layout.render(prefix, (
                    details, f"距上次阶段更新 {ProgressDuration(now - updated)}",
                )))
                self._emitted_at = now
            elif self._latest is not None and self._latest.state != "completed":
                self._emit(self._latest, now, waiting=True)

    def _stage_label(self, progress: TomorrowTrainingProgress) -> str:
        return "验证" if progress.validation is not None else _STAGE_LABELS[progress.stage]

    def _count(self, progress: TomorrowTrainingProgress) -> str:
        validation = progress.validation
        if validation is not None:
            return f"验证 {validation.completed_rows:>7}/{validation.total_rows:>7}行"
        counts = progress.fit_counts
        if counts is not None:
            label = "行业" if progress.count_unit == "industries" else "任务"
            if not progress.total_is_exact or counts.total == 0:
                return f"{label}已处理 {counts.processed}"
            return f"{label} {counts.processed}/{counts.total} ({counts.processed / counts.total:.1%})"
        if progress.stage == "history_conversion":
            return f"扫描 {progress.completed_units:>9}行"
        if progress.stage == "cross_section_conversion":
            return f"生成 {progress.produced_units:>9}样本行"
        if progress.stage in {"sample_cache_check", "sample_cache_seal"}:
            if not progress.total_is_exact or progress.total_units == 0:
                return f"已处理 {progress.completed_units / 1024**2:.1f}MiB"
            return f"{progress.completed_units / 1024**2:.1f}/{progress.total_units / 1024**2:.1f}MiB"
        label = "行业" if progress.count_unit == "industries" else "任务"
        if not progress.total_is_exact or progress.total_units == 0:
            return f"{label}已处理 {progress.completed_units}"
        return f"{label} {progress.completed_units:>6}/{progress.total_units:<6}"

    def _observe_epoch(self, progress: TomorrowTrainingProgress, now: float) -> None:
        detail = progress.fit_detail
        if (
            progress.validation is not None or detail is None or detail.phase != "training"
            or detail.epoch == 0 or detail.total_batches == 0
        ):
            self._eta_key = None
            self._eta_samples.clear()
            return
        key = (progress.stage, progress.strategy, detail.industry, detail.phase, detail.epoch)
        if key != self._eta_key or self._eta_samples and detail.completed_batches < self._eta_samples[-1][1]:
            self._eta_key = key
            self._eta_samples.clear()
        if not self._eta_samples or detail.completed_batches != self._eta_samples[-1][1]:
            self._eta_samples.append((now, detail.completed_batches))
        while len(self._eta_samples) > 2 and now - self._eta_samples[0][0] > 60:
            self._eta_samples.popleft()

    def _epoch_eta(self, now: float, progress: TomorrowTrainingProgress) -> str:
        detail = progress.fit_detail
        if detail is None or self._eta_key is None or detail.completed_batches == detail.total_batches:
            return ""
        if len(self._eta_samples) < 2 or now - self._eta_samples[-1][0] > 10:
            return "本轮估算中"
        first_time, first_batch = self._eta_samples[0]
        elapsed = now - first_time
        advanced = detail.completed_batches - first_batch
        if elapsed <= 0 or advanced <= 0:
            return "本轮估算中"
        remaining = detail.total_batches - detail.completed_batches
        seconds = remaining * elapsed / advanced
        return "本轮余约 <1秒" if seconds < 1 else f"本轮余约 {ProgressDuration(seconds)}"

    def _emit(self, progress: TomorrowTrainingProgress, now: float, *, waiting: bool = False) -> None:
        label = self._stage_label(progress)
        if progress.strategy is not None:
            label = f"{_HEAD_LABELS[progress.strategy]}·{label}"
        key = _display_key(progress)
        prefix = ProgressPrefix(now - self._started_at, f"{self._command_label}·{label}",
                                now - self._display_started[key], "等待" if waiting else _STATE_LABELS[progress.state],
                                stage_width=36)
        details = [self._count(progress)]
        if progress.stage == "history_conversion":
            details.append(f"生成 {progress.produced_units}样本行")
        if progress.validation is not None:
            details.append(f"有效模型 {progress.produced_units}")
        elif progress.stage == "sample_cache" and progress.state == "completed":
            details.append("复用完整样本" if progress.produced_units else "需要生成样本")
        counts = progress.fit_counts
        if counts is not None:
            details.append(f"成功 {counts.fitted} 跳过 {counts.skipped}")
        fit = progress.fit_detail
        if fit is not None:
            industry_name = fit.industry_name
            details.append(f"{industry_name}·{_FIT_PHASE_LABELS[fit.phase]}" if industry_name else _FIT_PHASE_LABELS[fit.phase])
            details.append(f"样本 {fit.training_rows}行")
            if fit.total_epochs:
                details.append(f"轮次 {fit.epoch}/{fit.total_epochs}")
            if fit.total_batches:
                details.append(f"本轮批次 {fit.completed_batches}/{fit.total_batches} ({fit.completed_batches / fit.total_batches:.1%})")
            eta = self._epoch_eta(now, progress)
            if eta:
                details.append(eta)
        if waiting and now - self._advanced_at >= _HEARTBEAT_INTERVAL_SECONDS:
            details.append(f"未推进 {ProgressDuration(now - self._advanced_at)}")
        _write_line(self._layout.render(prefix, tuple(details)))
        self._emitted_at = now


def _write_line(line: str) -> None:
    # Keep a complete wrapped event in one stream write across independent profile emitters.
    sys.stderr.write(line.rstrip(" ") + "\n")
    sys.stderr.flush()


def _fit_position(detail: TrainingFitDetail | None) -> tuple[str, TrainingFitPhase, int, int] | None:
    return None if detail is None else (detail.industry, detail.phase, detail.epoch, detail.completed_batches)


def _display_key(progress: TomorrowTrainingProgress) -> tuple[str, Strategy | None]:
    return (f"{progress.stage}:validation" if progress.validation is not None else progress.stage, progress.strategy)


def _fit_counts_position(progress: TomorrowTrainingProgress) -> tuple[int, int, int] | None:
    counts = progress.fit_counts
    return None if counts is None else (counts.processed, counts.fitted, counts.skipped)


def _has_advanced(previous: TomorrowTrainingProgress | None, current: TomorrowTrainingProgress) -> bool:
    if previous is None:
        return True
    return (
        previous.stage != current.stage or previous.strategy is not current.strategy
        or previous.completed_units != current.completed_units or previous.produced_units != current.produced_units
        or (None if previous.validation is None else previous.validation.completed_rows)
        != (None if current.validation is None else current.validation.completed_rows)
        or _fit_position(previous.fit_detail) != _fit_position(current.fit_detail)
        or _fit_counts_position(previous) != _fit_counts_position(current)
        or current.state == "completed" and previous.state != "completed"
    )


__all__ = [
    "ReadTrainingProgressLayout",
    "StderrTomorrowTrainingProgress",
    "TrainingProgressLayout",
]
