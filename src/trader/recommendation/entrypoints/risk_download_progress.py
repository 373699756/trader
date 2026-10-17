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
    "stopped": "待续传",
}


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
        percent = 100.0 if progress.total_units == 0 else progress.completed_units / progress.total_units * 100.0
        parts = [
            _format_duration(self._monotonic() - self._started),
            "risk_download 下载进度",
            f"波次 {progress.wave}",
            _STAGE_LABELS[progress.stage],
            _STATE_LABELS[progress.state],
            f"阶段 {progress.completed_units:>6}/{progress.total_units:>6}（{percent:6.2f}%）",
            f"请求 {progress.requested_codes:>6}",
        ]
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
        if progress.stage == "official_announcements" or progress.official_announcements_saved:
            parts.append(f"公告保存 {progress.official_announcements_saved:>6}")
        if progress.stop_reason is not None:
            parts.append(f"停止原因 {progress.stop_reason}")
        with self._lock:
            self._stream.write(" | ".join(parts) + "\n")
            self._stream.flush()


def _format_duration(seconds: float) -> str:
    hours, remainder = divmod(int(max(0.0, seconds)), 3600)
    minutes, remaining_seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{remaining_seconds:02d}"


__all__ = ["StderrRiskDownloadProgress"]
