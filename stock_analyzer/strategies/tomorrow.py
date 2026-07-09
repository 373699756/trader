from __future__ import annotations

from ..scoring import score_tomorrow_candidates


def score_tomorrow_picks(*args, **kwargs):
    return score_tomorrow_candidates(*args, **kwargs)

