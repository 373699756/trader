from __future__ import annotations

from ..scoring import score_today_candidates


def score_today_picks(*args, **kwargs):
    return score_today_candidates(*args, **kwargs)

