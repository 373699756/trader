"""Ordered, explicit selection of the scoring profiles hosted or trained together."""

from trader.recommendation.domain.scoring.profile_identity import ParseScoringProfile, ScoringProfileId


def ParseProfileSelection(value: str) -> tuple[ScoringProfileId, ...]:
    """Parse a comma-separated selection, rejecting empty and duplicate profiles."""
    profiles = tuple(ParseScoringProfile(part.strip()) for part in value.split(","))
    if len(set(profiles)) != len(profiles):
        raise ValueError("profile selection must not contain duplicates")
    return profiles


__all__ = ["ParseProfileSelection"]
