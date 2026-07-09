from __future__ import annotations

from ..scoring import score_swing_candidates


def score_swing_2_5d_picks(*args, **kwargs):
    return score_swing_candidates(*args, **kwargs)

