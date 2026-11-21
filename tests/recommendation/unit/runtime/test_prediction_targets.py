"""One price-event contract for configuration, fitting and settlement."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from trader.infra.settings.parser import ConfigurationError
from trader.recommendation.domain.outcomes.prediction_target import (
    EvaluatePredictionTarget,
    ParsePredictionTargetIdentity,
    PredictionTarget,
)
from trader.recommendation.domain.outcomes.tomorrow_target import TomorrowCloseGain
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.domain.scoring.profile_identity import SCORING_PROFILE_IDS
from trader.recommendation.infra.settings.loading import LoadPredictionTargets
from tests.helpers.recommendation.configuration import PROJECT_ROOT


def test_configuration_groups_only_matching_tomorrow_targets(tmp_path):
    raw = json.loads((PROJECT_ROOT / "config/selection.json").read_text())
    assert set(raw["prediction_targets"]) == set(SCORING_PROFILE_IDS)
    config_path = tmp_path / "strategy.json"
    selection = tmp_path / "selection.json"
    selection.write_text(json.dumps(raw))
    defaults = LoadPredictionTargets(config_path)
    assert defaults.tomorrow_groups(SCORING_PROFILE_IDS) == ((PredictionTarget(Strategy.TOMORROW, 500), SCORING_PROFILE_IDS),)
    assert all(defaults.for_profile(profile, Strategy.D25).threshold_bps == 100 for profile in SCORING_PROFILE_IDS)
    raw["prediction_targets"]["v1"]["tomorrow_bps"] = 600
    raw["prediction_targets"]["v5"]["d25_bps"] = 150
    selection.write_text(json.dumps(raw))
    targets = LoadPredictionTargets(config_path)
    assert targets.tomorrow_groups(SCORING_PROFILE_IDS) == (
        (PredictionTarget(Strategy.TOMORROW, 600), ("v1",)),
        (PredictionTarget(Strategy.TOMORROW, 500), ("v2", "v3", "v5")),
    )
    assert tuple(targets.for_profile(profile, Strategy.D25).threshold_bps for profile in SCORING_PROFILE_IDS) == (100, 100, 100, 150)


@pytest.mark.parametrize("invalid", [-1, True, 1.5, "500", None])
def test_invalid_target_is_rejected(tmp_path, invalid):
    raw = json.loads((PROJECT_ROOT / "config/selection.json").read_text())
    raw["prediction_targets"]["v3"]["d25_bps"] = invalid
    (tmp_path / "selection.json").write_text(json.dumps(raw))
    with pytest.raises(ConfigurationError, match=r"prediction_targets\.v3\.d25_bps"):
        LoadPredictionTargets(tmp_path / "strategy.json")


def test_inclusive_target_uses_exact_prices_and_requires_complete_window():
    signal = Decimal("100")
    tomorrow = PredictionTarget(Strategy.TOMORROW, 500)
    d25 = PredictionTarget(Strategy.D25, 100)
    closes = tuple(map(Decimal, ("100", "101", "101", "102")))
    assert EvaluatePredictionTarget(tomorrow, signal, (Decimal("105"),)) is TomorrowCloseGain.HIT
    assert EvaluatePredictionTarget(tomorrow, signal, (Decimal("104.9999"),)) is TomorrowCloseGain.MISS
    assert EvaluatePredictionTarget(d25, signal, closes) is TomorrowCloseGain.HIT
    assert EvaluatePredictionTarget(d25, signal, (*closes[:3], Decimal("101.9999"))) is TomorrowCloseGain.MISS
    assert EvaluatePredictionTarget(d25, signal, (*closes[:3], None)) is TomorrowCloseGain.PENDING
    assert EvaluatePredictionTarget(d25, signal, closes[:3]) is TomorrowCloseGain.PENDING
    assert ParsePredictionTargetIdentity(tomorrow.identity) == tomorrow
    assert ParsePredictionTargetIdentity(d25.identity) == d25
