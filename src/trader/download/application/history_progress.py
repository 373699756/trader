"""Best-effort typed history progress at application and I/O boundaries."""

from trader.download.domain.history_sync import (
    HistorySyncProgress, HistorySyncProgressPort, HistorySyncProgressStage, HistorySyncProgressState,
)


def PublishHistoryProgress(  # noqa: PLR0913 - typed progress projection, optional preparation capacity
    progress: HistorySyncProgressPort | None,
    stage: HistorySyncProgressStage,
    state: HistorySyncProgressState,
    counts: tuple[int, int] = (0, 1),
    current_item: str | None = None,
    *,
    required_free_bytes: int | None = None,
) -> None:
    if progress is None:
        return
    try:
        progress.publish(
            HistorySyncProgress(stage, state, *counts, current_item, required_free_bytes=required_free_bytes)
        )
    except OSError:
        pass


__all__ = ["PublishHistoryProgress"]
