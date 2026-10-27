from __future__ import annotations

import pytest

from trader.recommendation.domain.scoring.profile_identity import (
    SCORING_PROFILE_IDS,
    ParseScoringProfile,
)


def test_profile_identity_includes_production_profiles_and_v5_shadow() -> None:
    assert SCORING_PROFILE_IDS == ("v1", "v2", "v3", "v5")
    assert tuple(ParseScoringProfile(value) for value in SCORING_PROFILE_IDS) == SCORING_PROFILE_IDS


def test_profile_identity_rejects_unknown_values() -> None:
    with pytest.raises(ValueError, match="scoring profile"):
        ParseScoringProfile("latest")
