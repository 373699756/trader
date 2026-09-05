"""Stable identity and parsing for the user-selected scoring profiles."""

from __future__ import annotations

from typing import Literal

ScoringProfileId = Literal["v1", "v2", "v3"]
SCORING_PROFILE_IDS: tuple[ScoringProfileId, ...] = ("v1", "v2", "v3")


def parse_scoring_profile(value: str) -> ScoringProfileId:
    if value not in SCORING_PROFILE_IDS:
        raise ValueError("scoring profile must be v1, v2, or v3")
    return value


__all__ = ["SCORING_PROFILE_IDS", "ScoringProfileId", "parse_scoring_profile"]
