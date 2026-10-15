"""Stable identity and parsing for the user-selected scoring profiles."""

from __future__ import annotations

from typing import Literal

ScoringProfileId = Literal["v2", "v3"]
SCORING_PROFILE_IDS: tuple[ScoringProfileId, ...] = ("v2", "v3")


def ParseScoringProfile(value: str) -> ScoringProfileId:
    if value not in SCORING_PROFILE_IDS:
        raise ValueError("scoring profile must be v2 or v3")
    return value


__all__ = ["SCORING_PROFILE_IDS", "ScoringProfileId", "ParseScoringProfile"]
