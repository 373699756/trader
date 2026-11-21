"""Strict profile-specific fitting settings at the training command boundary."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from trader.infra.settings.parser import (
    Integer,
    ProfileMapping,
    ReadJsonObject,
    RequireExactKeys,
    RequireMapping,
)
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.domain.scoring.profile_identity import ScoringProfileId
from trader.training.domain.v1.learned_rank import V1TrainingSettings
from trader.training.infra.d25.contracts import StandardD25Profile
from trader.training.infra.fitting_settings import LoadIndustryFittingSettings, ModelFittingParameters
from trader.training.infra.learned_rank_settings import LoadV1TrainingSettings

if TYPE_CHECKING:
    from trader.training.infra.model_bundles.contracts import TrainedProfileContract


@dataclass(frozen=True)
class ProfileTrainingSettings:
    fit_dates: int
    history_sessions: int
    momentum_horizons: tuple[int, ...]
    market_state_momentum_horizons: tuple[int, ...]
    tomorrow_feature_ids: tuple[str, ...]
    d25_feature_ids: tuple[str, ...]
    fitting: ModelFittingParameters

    def require_profile(self, profile: TrainedProfileContract) -> None:
        if (
            self.history_sessions != profile.history_sessions
            or self.momentum_horizons != profile.momentum_horizons
            or self.market_state_momentum_horizons != profile.market_state_momentum_horizons
            or any(
                (self.tomorrow_feature_ids if head.strategy is Strategy.TOMORROW else self.d25_feature_ids)
                != tuple(name for name in head.feature_manifest.names if not name.startswith("sequence_"))
                for head in profile.heads
            )
        ):
            raise ValueError("training input settings do not match the profile feature contract")


def LoadProfileTrainingSettings(project_root: Path, profile_id: ScoringProfileId) -> ProfileTrainingSettings:
    """Read the inputs for explicit paired close-return diagnostics."""
    return _load_profile_training_settings(project_root, profile_id, paired=True)


def LoadD25TrainingSettings(project_root: Path, profile_id: ScoringProfileId) -> ProfileTrainingSettings:
    settings = _load_profile_training_settings(project_root, profile_id, paired=False)
    settings.require_profile(StandardD25Profile(profile_id))
    return settings


def _load_profile_training_settings(
    project_root: Path, profile_id: ScoringProfileId, *, paired: bool,
) -> ProfileTrainingSettings:
    fit_dates, fitting = LoadIndustryFittingSettings(project_root, profile_id)
    features = ReadJsonObject(project_root / "config" / "features.json")
    RequireExactKeys(features, {"model_contracts", "factor_index", "factor_contract", "news_signal"}, "features")
    inputs = ProfileMapping(features.get("model_contracts"), profile_id, "features.model_contracts")
    RequireExactKeys(
        inputs,
        {"history_sessions", "momentum_horizons", "market_state_momentum_horizons", "heads"},
        f"features.model_contracts.{profile_id}",
    )
    heads = RequireMapping(inputs, "heads")
    RequireExactKeys(heads, {"tomorrow", "d25"}, f"features.model_contracts.{profile_id}.heads")
    return ProfileTrainingSettings(
        fit_dates,
        Integer(inputs, "history_sessions", minimum=61),
        _positive_integer_tuple(
            inputs.get("momentum_horizons"), f"features.model_contracts.{profile_id}.momentum_horizons"
        ),
        _positive_integer_tuple(
            inputs.get("market_state_momentum_horizons"),
            f"features.model_contracts.{profile_id}.market_state_momentum_horizons",
            allow_empty=True,
        ),
        _feature_ids(heads.get("tomorrow"), f"features.model_contracts.{profile_id}.heads.tomorrow") if paired else (),
        _feature_ids(heads.get("d25"), f"features.model_contracts.{profile_id}.heads.d25"),
        fitting,
    )


def _positive_integer_tuple(raw: object, label: str, *, allow_empty: bool = False) -> tuple[int, ...]:
    if (
        not isinstance(raw, list)
        or (not raw and not allow_empty)
        or any(not isinstance(item, int) or isinstance(item, bool) or item < 1 for item in raw)
        or raw != sorted(set(raw))
    ):
        raise ValueError(f"{label} must contain sorted unique positive integers")
    return tuple(raw)


def _feature_ids(raw: object, label: str) -> tuple[str, ...]:
    if not isinstance(raw, list) or not raw or any(not isinstance(item, str) or not item for item in raw):
        raise ValueError(f"{label} must contain non-empty feature ids")
    values = tuple(raw)
    if len(values) != len(set(values)):
        raise ValueError(f"{label} must contain unique feature ids")
    return values


def LoadRuntimeTrainingSettings(
    project_root: Path, profile_id: ScoringProfileId
) -> ProfileTrainingSettings | V1TrainingSettings:
    if profile_id == "v1":
        return LoadV1TrainingSettings(project_root)
    return LoadD25TrainingSettings(project_root, profile_id)


__all__ = ["ProfileTrainingSettings", "LoadD25TrainingSettings", "LoadProfileTrainingSettings", "LoadRuntimeTrainingSettings"]
