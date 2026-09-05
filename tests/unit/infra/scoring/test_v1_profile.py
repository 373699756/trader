from __future__ import annotations

import json
from importlib import resources

import pytest

from trader.application.ports.model_scoring import ModelInput
from trader.infra.scoring.profiles.v1.artifact_codec import decode_v1_tomorrow_artifact
from trader.infra.scoring.profiles.v1.profile import build_v1_tomorrow_predictor


def _document() -> dict[str, object]:
    raw = json.loads(
        resources.files("trader.resources.models").joinpath("tomorrow_v1_model.json").read_text(encoding="utf-8")
    )
    assert isinstance(raw, dict)
    return raw


def test_v1_codec_and_predictor_preserve_authorized_identity_and_golden_prediction() -> None:
    predictor = build_v1_tomorrow_predictor(decode_v1_tomorrow_artifact(_document()))

    prediction = predictor.predict((ModelInput("600000", (0.01, -0.02, 0.03)),))[0]

    assert predictor.profile_id == "v1"
    assert predictor.model_id == "v1_manual_residual_momentum_v1"
    assert predictor.model_hash == "4291ea514c233a14ab6f9262e72ea541d1e9a794e73d02f10f8220509f6f502b"
    assert prediction.predicted_excess_return == pytest.approx(-2.5136160956193677e-05)
    assert prediction.model_disagreement == 0.0
    assert predictor.profile_evidence.historical_status == "historical_unavailable"
    assert predictor.profile_evidence.activation_basis == "manual_user_override"


def test_v1_codec_rejects_modified_payload_with_the_authorized_hash() -> None:
    document = _document()
    document["linear_intercept"] = 1.0

    with pytest.raises(ValueError, match="hash is not authorized"):
        decode_v1_tomorrow_artifact(document)
