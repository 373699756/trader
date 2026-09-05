from __future__ import annotations

import json
from importlib import resources

import pytest

from trader.application.ports.model_scoring import ModelInput
from trader.infra.scoring.profiles.v2.artifact_codec import decode_v2_tomorrow_artifact
from trader.infra.scoring.profiles.v2.profile import build_v2_tomorrow_predictor


def _document() -> dict[str, object]:
    raw = json.loads(
        resources.files("trader.resources.models").joinpath("tomorrow_p2_model.json").read_text(encoding="utf-8")
    )
    assert isinstance(raw, dict)
    return raw


def test_v2_codec_and_predictor_preserve_authorized_identity_and_golden_prediction() -> None:
    predictor = build_v2_tomorrow_predictor(decode_v2_tomorrow_artifact(_document()))
    row = ModelInput("600000", (0.01, 0.02, 0.03, 0.01, -0.02, 0.03))

    prediction = predictor.predict((row,))[0]

    assert predictor.profile_id == "v2"
    assert predictor.model_id == "daily_reconstructible_ensemble_v1"
    assert predictor.model_hash == "27034e52813f1776e2ed218c1c397f481b244fb852b01be08ddc21249d887da5"
    assert prediction.predicted_excess_return == pytest.approx(-3.2489670901064623e-07)
    assert prediction.model_disagreement == pytest.approx(0.00015194486578771707)
    assert predictor.profile_evidence.historical_status == "historical_rejected"
    assert predictor.profile_evidence.activation_basis == "manual_user_override"


def test_v2_codec_rejects_modified_packaged_artifact() -> None:
    document = _document()
    document["linear_intercept"] = 1.0

    with pytest.raises(ValueError, match="content hash is invalid"):
        decode_v2_tomorrow_artifact(document)
