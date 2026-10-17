"""Compact stderr progress for the explicit company-risk download command."""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
from threading import Event, Lock, Thread
from typing import TextIO

from trader.recommendation.application.risk_download import RiskDownloadProgress

_STAGE_LABELS = {
    "local_snapshot": "本地日库",
    "research_initial": "公司研究",
    "research_retry": "研究重试",
    "official_announcements": "巨潮公告",
    "wave_summary": "波次汇总",
    "finished": "下载结果",
}
_STATE_LABELS = {
    "started": "开始",
    "running": "进行中",
    "completed": "完成",
    "deferred": "本波截止",
    "waiting": "等待日库",
    "stopped": "待续传",
}
_WAVE_STAGES = frozenset({"research_initial", "research_retry", "official_announcements"})


class StderrRiskDownloadProgress:
    def __init__(
        self,
        stream: TextIO | None = None,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        heartbeat_seconds: float = 20.0,
        command_name: str = "risk_download",
    ) -> None:
        if heartbeat_seconds <= 0.0:
            raise ValueError("heartbeat_seconds must be positive")
        self._stream = stream if stream is not None else sys.stderr
        self._command_name = command_name
        self._monotonic = monotonic
        self._started = monotonic()
        self._lock = Lock()
        self._heartbeat_seconds = heartbeat_seconds
        self._last_status_at = self._started
        self._last_output_at = self._started
        self._current_stage = "启动准备"
        self._last_running_key: tuple[int, str] | None = None
        self._last_running_count = 0
        self._closed = Event()
        self._thread: Thread | None = None

    def start(self) -> None:
        with self._lock:
            if self._thread is not None:
                raise RuntimeError("risk download progress already started")
            now = self._monotonic()
            self._write_locked(
                f"{_format_duration(now - self._started)} | {self._command_name} 下载进度 | 启动准备 | 开始", now
            )
            thread = Thread(target=self._run_heartbeat, name="risk-download-progress")
            thread.start()
            self._thread = thread

    def close(self) -> None:
        self._closed.set()
        thread = self._thread
        if thread is not None:
            thread.join()

    def _run_heartbeat(self) -> None:
        while not self._closed.wait(self._heartbeat_seconds):
            with self._lock:
                now = self._monotonic()
                if now - self._last_output_at < self._heartbeat_seconds:
                    continue
                age = max(0, int(now - self._last_status_at))
                self._write_locked(
                    f"{_format_duration(now - self._started)} | {self._command_name} 下载进度 | "
                    f"{self._current_stage} | 仍在运行 | 距上次状态更新 {age}秒",
                    now,
                )

    def _write_locked(self, line: str, now: float) -> None:
        self._stream.write(line + "\n")
        self._stream.flush()
        self._last_output_at = now

    def publish(self, progress: RiskDownloadProgress) -> None:
        now = self._monotonic()
        if progress.state == "running" and progress.stage in _WAVE_STAGES:
            with self._lock:
                key = (progress.wave, progress.stage)
                if (
                    key == self._last_running_key
                    and progress.completed_units - self._last_running_count < 25
                    and now - self._last_output_at < 5.0
                ):
                    self._last_status_at = now
                    return
                self._last_running_key = key
                self._last_running_count = progress.completed_units
        parts = [
            _format_duration(now - self._started),
            f"{self._command_name} 下载进度",
            f"波次 {progress.wave}",
            _STAGE_LABELS[progress.stage],
            _STATE_LABELS[progress.state],
        ]
        if progress.stage not in _WAVE_STAGES and not (
            progress.stage == "local_snapshot" and progress.state == "started"
        ):
            parts.append(_format_progress("总体", progress.completed_units, progress.total_units))
        if progress.research_pending_codes is not None:
            parts.append(
                _format_progress(
                    "研究累计",
                    max(0, progress.requested_codes - progress.research_pending_codes),
                    progress.requested_codes,
                )
            )
        if progress.official_pending_codes is not None:
            parts.append(
                _format_progress(
                    "巨潮累计",
                    max(0, progress.requested_codes - progress.official_pending_codes),
                    progress.requested_codes,
                )
            )
        if progress.stage in _WAVE_STAGES:
            parts.append(
                f"本波待处理 {progress.total_units:>6}"
                if progress.state == "started" or (progress.state == "waiting" and progress.completed_units == 0)
                else _format_progress("本波处理", progress.completed_units, progress.total_units)
            )
        parts.append(f"请求 {progress.requested_codes:>6}")
        if progress.research_pending_codes is not None:
            parts.append(f"研究待补 {progress.research_pending_codes:>6}")
        if progress.official_pending_codes is not None:
            parts.append(f"巨潮待补 {progress.official_pending_codes:>6}")
        if progress.available_codes is not None:
            parts.append(f"可用 {progress.available_codes:>6}")
        if progress.covered_codes is not None:
            parts.append(f"四项有值 {progress.covered_codes:>6}")
        if progress.deferred_codes is not None:
            parts.append(f"延后 {progress.deferred_codes:>6}")
        if progress.changed_codes is not None:
            parts.append(f"变更 {progress.changed_codes:>6}")
        if progress.stage == "official_announcements" or progress.official_pages_fetched:
            parts.append(f"官方分页 {progress.official_pages_fetched:>6}")
        if progress.stage == "official_announcements" or progress.official_pages_checkpointed:
            parts.append(f"断点分页 {progress.official_pages_checkpointed:>6}")
        if progress.stage == "official_announcements" or progress.official_announcements_saved:
            parts.append(f"公告保存 {progress.official_announcements_saved:>6}")
        if progress.stage == "official_announcements" or progress.official_resumed_codes:
            parts.append(f"断点续传 {progress.official_resumed_codes:>6}")
        if progress.stage == "official_announcements" and progress.state in {"completed", "deferred", "waiting"}:
            parts.append(f"来源耗时 {progress.official_sync_seconds:.2f}秒")
            parts.append(
                "分项累计 "
                f"请求{progress.official_request_seconds:.2f}/"
                f"解析{progress.official_parse_seconds:.2f}/"
                f"公告写入{progress.official_evidence_write_seconds:.2f}/"
                f"断点写入{progress.official_cursor_write_seconds:.2f}秒"
            )
            parts.append(f"恢复耗时 {progress.official_recovery_seconds:.2f}秒")
        if progress.official_persistence_error == "busy":
            parts.append("日库写入 等待写锁")
        elif progress.official_persistence_error == "unavailable":
            parts.append("日库写入 不可用")
        if progress.official_source_error == "access_denied":
            parts.append("巨潮来源 拒绝访问")
        elif progress.official_source_error == "rate_limited":
            parts.append("巨潮来源 限流")
        if progress.stop_reason is not None:
            parts.append(f"停止原因 {progress.stop_reason}")
        with self._lock:
            self._current_stage = _STAGE_LABELS[progress.stage]
            self._last_status_at = now
            self._write_locked(" | ".join(parts), now)


def _format_duration(seconds: float) -> str:
    hours, remainder = divmod(int(max(0.0, seconds)), 3600)
    minutes, remaining_seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{remaining_seconds:02d}"


def _format_progress(label: str, completed: int, total: int) -> str:
    percent = 100.0 if total == 0 else completed / total * 100.0
    return f"{label} {completed:>6}/{total:>6}（{percent:6.2f}%）"


__all__ = ["StderrRiskDownloadProgress"]
