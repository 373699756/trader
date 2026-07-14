"""Public scoring API backed by explicit scoring-core entry points."""

from __future__ import annotations

from .scoring_core.candidate_filters import candidate_filter_report, prepare_candidates
from .scoring_core.market_regime import build_market_regime
from .scoring_core.theme_limits import limit_theme_concentration
from .strategies import (
    score_swing_2_5d_picks as score_swing_candidates,
    score_today_picks as score_today_candidates,
    score_tomorrow_picks as score_tomorrow_candidates,
)


__all__ = [
    "build_market_regime",
    "candidate_filter_report",
    "limit_theme_concentration",
    "prepare_candidates",
    "score_today_candidates",
    "score_tomorrow_candidates",
    "score_swing_candidates",
]
