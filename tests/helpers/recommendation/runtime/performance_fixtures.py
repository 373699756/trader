"""Profile-complete synthetic workload checks without fitting or external I/O."""

from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from trader.infra.settings.models import (
    DatabasePerformanceSettings,
    PerformanceBudgetSettings,
    PerformanceMemorySettings,
    PerformanceRoundSettings,
    PerformanceWorkloadSettings,
)
from trader.recommendation.application.ports.loaded_profile import (
    HeadRuntime,
    LoadedScoringProfile,
    ModelInput,
    ModelPrediction,
    ProfileEvidence,
)
from trader.recommendation.domain.market.v1.features import LEARNED_RANK_FEATURE_IDS
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.domain.scoring.profile_identity import ScoringProfileId
from trader.recommendation.domain.scoring.residualization import TRAINED_HEAD_EXPOSURE_CONTRACT
from trader.training.infra.profile.composition import SingleHeadCombiner
from trader.training.infra.profile.v2.contracts import V2_TRAINING_PROFILE
from trader.training.infra.profile.v3.contracts import V3_TRAINING_PROFILE
from trader.training.infra.profile.v5.contracts import V5_TRAINING_PROFILE
from trader.training.infra.training_settings import LoadRuntimeTrainingSettings


@dataclass(frozen=True)
class _Predictor:
    profile_id: ScoringProfileId
    feature_ids: tuple[str, ...]
    industry_ids: tuple[str, ...] = ("capco:fixture-bank", "capco:fixture-energy")
    model_id: str = "performance-model"
    model_hash: str = "a" * 64
    exposure_contract = TRAINED_HEAD_EXPOSURE_CONTRACT

    def predict(self, inputs: tuple[ModelInput, ...]) -> tuple[ModelPrediction, ...]:
        raise AssertionError("fixture qualification must not call prediction")


def _profile(profile_id: ScoringProfileId) -> LoadedScoringProfile:
    contracts = {"v2": V2_TRAINING_PROFILE, "v3": V3_TRAINING_PROFILE, "v5": V5_TRAINING_PROFILE}
    heads: dict[Strategy, HeadRuntime] = {}
    for strategy in (Strategy.TOMORROW, Strategy.D25):
        names = (
            LEARNED_RANK_FEATURE_IDS
            if profile_id == "v1"
            else tuple(
                name
                for name in contracts[profile_id].head_for_strategy(strategy).feature_manifest.names
                if not name.startswith("sequence_")
            )
        )
        heads[strategy] = HeadRuntime(
            strategy,
            _Predictor(profile_id, names),
            SingleHeadCombiner(),
            ProfileEvidence("historical_unavailable", (), "trained_artifact", production_authority=profile_id != "v1"),
        )
    return LoadedScoringProfile(profile_id, heads)


def _history_sessions(profile_id: ScoringProfileId) -> int:
    return LoadRuntimeTrainingSettings(Path.cwd(), profile_id).history_sessions


def _budgets() -> PerformanceBudgetSettings:
    return PerformanceBudgetSettings(
        PerformanceWorkloadSettings(12, 12),
        PerformanceRoundSettings(1, 5),
        {},
        {},
        PerformanceMemorySettings(536870912, 2147483648, 5.0),
        10.0,
        DatabasePerformanceSettings(10.0, 15.0),
    )
