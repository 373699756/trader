import time
from trader.download.application.read_published_history import ReadPublishedHistoryUseCase
from trader.infra.settings.models import HistoryPreloadSettings
from trader.recommendation.infra.market_data.published_history_cache import PublishedHistoryCache
from trader.recommendation.infra.market_data.shared_history_projection import SharedHistoryProjection


def _cache(source, *, outcomes=None, monotonic=time.monotonic):
    return PublishedHistoryCache(
        ReadPublishedHistoryUseCase(source),
        lookback_sessions=61,
        monotonic=monotonic,
        outcome_history=ReadPublishedHistoryUseCase(outcomes) if outcomes else None,
        projection_owner=SharedHistoryProjection(ReadPublishedHistoryUseCase(source), monotonic=monotonic, settings=HistoryPreloadSettings(warn_after_seconds=300.0)),
    )
