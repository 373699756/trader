"""Shared streaming preload and profile-specific window contracts."""

from tests.helpers.recommendation.market.candidate_history_tail import CALENDAR
from trader.download.application.read_published_history import ReadPublishedHistoryUseCase
from trader.infra.settings.models import HistoryPreloadSettings
from trader.recommendation.infra.market_data.published_history_cache import PublishedHistoryCache
from trader.recommendation.infra.market_data.shared_history_projection import SharedHistoryProjection


def _cache(source, sessions, owner=None, rank=False, sequence=False, calendar=None):
    return PublishedHistoryCache(
        ReadPublishedHistoryUseCase(source),
        lookback_sessions=sessions,
        monotonic=lambda: 0.0,
        open_dates=calendar or (lambda: CALENDAR),
        learned_rank_inputs=rank,
        sequence_inputs=sequence,
        projection_owner=owner if owner is not None else SharedHistoryProjection(
            ReadPublishedHistoryUseCase(source), monotonic=lambda: 0.0,
            settings=HistoryPreloadSettings(warn_after_seconds=300.0),
        ),
    )
