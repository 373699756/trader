"""Stable identity and parsing for the user-selected scoring profiles."""

from __future__ import annotations

from typing import Literal

ScoringProfileId = Literal["v1", "v2", "v3", "v5"]
SCORING_PROFILE_IDS: tuple[ScoringProfileId, ...] = ("v1", "v2", "v3", "v5")
LEARNED_RANK_SCORE_SCALE = "learned_rank_percentile_0_100"
MODEL_PREDICTION_RANK_SCORE_SCALE = "model_prediction_percentile_0_100"
MODEL_RANK_SCORE_SCALES = frozenset((LEARNED_RANK_SCORE_SCALE, MODEL_PREDICTION_RANK_SCORE_SCALE))


def ParseScoringProfile(value: str) -> ScoringProfileId:
    if value not in SCORING_PROFILE_IDS:
        raise ValueError("scoring profile must be v1, v2, v3 or v5")
    return value


__all__ = [
    "LEARNED_RANK_SCORE_SCALE",
    "MODEL_PREDICTION_RANK_SCORE_SCALE",
    "MODEL_RANK_SCORE_SCALES",
    "SCORING_PROFILE_IDS",
    "ScoringProfileId",
    "ParseScoringProfile",
]
