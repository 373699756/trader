"""Compact stderr progress for the explicit company-risk download command."""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
from threading import Lock
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
    ) -> None:
        self._stream = stream if stream is not None else sys.stderr
        self._monotonic = monotonic
        self._started = monotonic()
        self._lock = Lock()

    def publish(self, progress: RiskDownloadProgress) -> None:
        parts = [
            _format_duration(self._monotonic() - self._started),
            "risk_download 下载进度",
            f"波次 {progress.wave}",
            _STAGE_LABELS[progress.stage],
            _STATE_LABELS[progress.state],
        ]
        if progress.stage not in _WAVE_STAGES:
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
            parts.append(f"完整 {progress.covered_codes:>6}")
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
        if progress.stop_reason is not None:
            parts.append(f"停止原因 {progress.stop_reason}")
        with self._lock:
            self._stream.write(" | ".join(parts) + "\n")
            self._stream.flush()


def _format_duration(seconds: float) -> str:
    hours, remainder = divmod(int(max(0.0, seconds)), 3600)
    minutes, remaining_seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{remaining_seconds:02d}"


def _format_progress(label: str, completed: int, total: int) -> str:
    percent = 100.0 if total == 0 else completed / total * 100.0
    return f"{label} {completed:>6}/{total:>6}（{percent:6.2f}%）"


__all__ = ["StderrRiskDownloadProgress"]
