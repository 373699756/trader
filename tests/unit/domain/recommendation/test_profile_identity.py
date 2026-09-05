from __future__ import annotations

import pytest

from trader.domain.recommendation.model_scoring.profile_identity import (
    SCORING_PROFILE_IDS,
    parse_scoring_profile,
)


def test_profile_identity_has_only_the_three_production_profiles() -> None:
    assert SCORING_PROFILE_IDS == ("v1", "v2", "v3")
    assert tuple(parse_scoring_profile(value) for value in SCORING_PROFILE_IDS) == SCORING_PROFILE_IDS


def test_profile_identity_rejects_unknown_values() -> None:
    with pytest.raises(ValueError, match="scoring profile"):
        parse_scoring_profile("latest")
