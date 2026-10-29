"""Artifact assembly does not grant V5 production authority."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

import pytest

from trader.recommendation.application.ports.loaded_profile import ModelInput, ModelPrediction
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.domain.scoring.profile_identity import ScoringProfileId
from trader.recommendation.domain.scoring.residualization import TRAINED_HEAD_EXPOSURE_CONTRACT, ExposureContract
from trader.recommendation.domain.selection.execution_cost import ExecutionCostPolicy
from trader.recommendation.infra.scoring.profile_factory import BuildProfileScoring
from trader.training.infra.model_bundles.bundle_codec import TrainedHeadModelBundle
from trader.training.infra.model_bundles.contracts import TrainedHeadContract, TrainedProfileContract
from trader.training.infra.model_bundles.profile import BuildTrainedScoringProfile
from trader.training.infra.profile.v2.contracts import V2_TRAINING_PROFILE
from trader.training.infra.profile.v3.contracts import V3_TRAINING_PROFILE
from trader.training.infra.profile.v5.contracts import V5_TRAINING_PROFILE


@dataclass(frozen=True)
class _Predictor:
    profile_id: ScoringProfileId
    bundle: TrainedHeadModelBundle
    strategy: Strategy

    @property
    def model_id(self) -> str:
        return self.bundle.model_id

    @property
    def model_hash(self) -> str:
        return self.bundle.content_hash

    @property
    def feature_ids(self) -> tuple[str, ...]:
        return tuple(name for name in self.bundle.feature_ids if not name.startswith("sequence_"))

    @property
    def exposure_contract(self) -> ExposureContract:
        return self.bundle.exposure_contract

    @property
    def industry_ids(self) -> tuple[str, ...]:
        return ("bank",)

    def predict(self, inputs: Sequence[ModelInput]) -> tuple[ModelPrediction, ...]:
        raise AssertionError("authority checks must not invoke model inference")


def _bundle(profile: TrainedProfileContract, contract: TrainedHeadContract) -> TrainedHeadModelBundle:
    return TrainedHeadModelBundle(
        serialization_profile_id=profile.profile_id,
        strategy=contract.strategy,
        model_id=contract.model_id,
        feature_ids=contract.feature_manifest.names,
        feature_units=contract.feature_manifest.units,
        exposure_contract=TRAINED_HEAD_EXPOSURE_CONTRACT,
        algorithm=profile.algorithm,
        training_input_scope="complete_manifest",
        training_input_hash="a" * 64,
        training_run_hash="b" * 64,
        label_cutoff=date(2026, 9, 8),
        source_identity_hash="c" * 64,
        training_input_document_hash="d" * 64,
        training_input_codes=1,
        training_universe_codes=1,
        split_hash="e" * 64,
        report_hash="f" * 64,
        feature_manifest_hash=contract.feature_manifest.content_hash,
        training_contract_hash="1" * 64,
        label_target=contract.label_target,
        historical_status="historical_data_insufficient",
        historical_failure_reasons=("daily_close_proxy_not_point_in_time",),
        training_anchor="15:00_close_proxy",
        runtime_anchor="15:00",
        point_in_time_parity=False,
        training_rows=20000,
        validation_rows=200,
        industries=(),
        dependencies=(),
        metadata_hash="2" * 64,
        content_hash="3" * 64,
    )


@pytest.mark.parametrize(
    ("profile", "authority", "activation_basis"),
    (
        (V2_TRAINING_PROFILE, True, "manual_user_override"),
        (V3_TRAINING_PROFILE, True, "manual_user_override"),
        (V5_TRAINING_PROFILE, True, "manual_user_override"),
    ),
)
def test_loaded_dual_head_authority_matches_artifact_admission(profile, authority, activation_basis, monkeypatch):
    # Codec validation has separate coverage; isolate assembly from optional tensor loading.
    monkeypatch.setattr("trader.training.infra.model_bundles.profile.TrainedHeadPredictor", _Predictor)
    loaded = BuildTrainedScoringProfile(profile.profile_id, tuple(_bundle(profile, head) for head in profile.heads))
    scorer = BuildProfileScoring(
        profile.profile_id,
        loaded,
        history_sessions=profile.history_sessions,
        model_failure="",
        cost_policy=ExecutionCostPolicy(0.002, 1.0),
        maintenance=None,
    )
    status = scorer.status()
    assert status is not None
    assert set(loaded.heads) == set(status.heads) == {Strategy.TOMORROW, Strategy.D25}
    for strategy, head in loaded.heads.items():
        assert head.evidence.production_authority is authority
        assert head.evidence.activation_basis == activation_basis
        assert status.heads[strategy].production_authority is authority
        assert status.heads[strategy].activation_basis == activation_basis
        batch = scorer.score(strategy, ())
        assert batch is not None
        assert batch.production_authority is authority
        assert not batch.predictions


@pytest.mark.parametrize(("profile_id", "authority"), (("v1", True), ("v2", True), ("v3", True), ("v5", True)))
def test_missing_dual_head_status_preserves_profile_authority(profile_id, authority):
    scorer = BuildProfileScoring(
        profile_id,
        None,
        history_sessions=62,
        model_failure="production_model_missing",
        cost_policy=ExecutionCostPolicy(0.002, 1.0),
        maintenance=None,
    )
    status = scorer.status()
    assert status is not None
    assert set(status.heads) == {Strategy.TOMORROW, Strategy.D25}
    for strategy, head in status.heads.items():
        assert head.is_enabled is False
        assert head.production_authority is authority
        assert head.activation_basis == (
            "manual_user_override" if profile_id in {"v1", "v5"} else "trained_artifact"
        )
        with pytest.raises(RuntimeError, match="production_model_missing"):
            scorer.score(strategy, ())
