"""Current stock requests and durable acknowledgements for one download phase."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

from trader.download.application.progress_message import ProgressField, ProgressMessage, ProgressTask
from trader.download.domain.history_sync import HistoryStockRequest, HistorySyncProgress
from trader.infra.progress_format import PadProgressText, ShortProgressText


@dataclass(frozen=True)
class _RequestTiming:
    request: HistoryStockRequest
    started: float


class HistoryDownloadDisplay:
    """Observe real supplier calls without counting local reads as requests."""

    def __init__(self, monotonic: Callable[[], float]) -> None:
        self._monotonic = monotonic
        self._latest: HistorySyncProgress | None = None
        self._requests: dict[str, _RequestTiming] = {}
        self._samples: deque[tuple[float, int]] = deque()
        self._completed = 0
        self._started: float | None = None
        self._confirmed_at = 0.0
        self._last_wait = float("-inf")
        self._active = False

    def update(self, progress: HistorySyncProgress) -> ProgressMessage | None:
        now = self._monotonic()
        if self._started is None:
            self._started = self._confirmed_at = now
            self._completed = progress.completed_units
            self._samples.append((now, self._completed))
        self._active = progress.tencent_summary is None and progress.state != "cancelled"
        self._completed = max(self._completed, progress.completed_units)
        if progress.request is None:
            self._latest = progress
        request = progress.request
        if request is not None:
            if request.state == "finished":
                self._requests.pop(request.code, None)
                return None
            self._requests[request.code] = _RequestTiming(request, now)
            return self._message(f"请求{request.code}-{request.name}", now)
        if progress.tencent_summary is not None or progress.state == "cancelled":
            self._requests.clear()
            return None
        if progress.current_item is not None and progress.state in {"completed", "failed"}:
            self._confirmed_at = now
            self._samples.append((now, self._completed))
            action = "失败确认" if progress.failure_reason else "确认" if progress.supplier_source else "复用确认"
            return self._message(f"{action}{progress.current_item}-{progress.current_name or ''}", now)
        if progress.state == "started":
            self._last_wait = now
            return self._message("开始", now)
        return None

    def finish(self) -> None:
        self._active = False
        self._requests.clear()

    def heartbeat_message(self) -> ProgressMessage | None:
        if not self._active or self._latest is None:
            return None
        now = self._monotonic()
        if now - self._last_wait < 5:
            return None
        oldest = min(self._requests.values(), key=lambda item: item.started, default=None)
        if oldest is not None:
            if now - oldest.started < 5:
                return None
            request = oldest.request
            action = f"等待{request.code}-{request.name}"
            age = f"请求等待{int(now - oldest.started)}秒"
        else:
            pipeline = self._latest.pipeline
            action = pipeline.write_operation if pipeline is not None and pipeline.writing_codes else "读库/校验"
            age = f"距确认{int(now - self._confirmed_at)}秒"
        self._last_wait = now
        return self._message(action, now, age=age)

    def _estimate(self, now: float) -> str:
        while len(self._samples) > 1 and self._samples[1][0] <= now - 180:
            self._samples.popleft()
        # Keep a boundary sample, then prorate only its straddling interval.
        started, first_count = self._samples[0]
        baseline = float(first_count)
        elapsed = now - started
        completed = self._completed - baseline
        if elapsed > 180 and len(self._samples) > 1:
            following, after = self._samples[1]
            if following > started:
                baseline += (after - baseline) * (now - 180 - started) / (following - started)
            elapsed = 180
            completed = self._completed - baseline
        if elapsed < 60 or completed < 10 or now - self._confirmed_at >= 180:
            return "速度/剩余估算中"
        rate = completed * 3600 / elapsed
        remaining = max(0, self._latest.total_units - self._completed) if self._latest is not None else 0
        seconds = remaining * 3600 / rate
        eta = f"{seconds / 3600:.1f}小时" if seconds >= 3600 else f"{seconds / 60:.0f}分钟"
        return f"近3分{rate:.0f}股/时 阶段剩余约{eta}"

    def _message(self, action: str, now: float, *, age: str | None = None) -> ProgressMessage:
        latest = self._latest
        if latest is None:
            raise RuntimeError("history_download_display_missing_start")
        pipeline = latest.pipeline
        queued = pipeline.queued_codes - pipeline.writing_codes if pipeline is not None else 0
        writing = pipeline.writing_codes if pipeline is not None else 0
        fields = (
            f"已确认{self._completed:6d}/{latest.total_units:6d}",
            PadProgressText(ShortProgressText(action, 26), 26),
            f"请求{len(self._requests):2d} 待写{queued:3d} 写入{writing:2d}",
            PadProgressText(age or f"距确认{int(now - self._confirmed_at)}秒", 16),
            self._estimate(now),
        )
        return ProgressMessage(
            tuple(ProgressField(field) for field in fields),
            task=ProgressTask("history", "prices", "Tencent下载"),
            state="处理中",
            immediate=True,
            compact=True,
        )


__all__ = ["HistoryDownloadDisplay"]
