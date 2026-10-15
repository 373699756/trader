"""Strict profile-specific fitting settings at the training command boundary."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from trader.infra.settings.parser import (
    Integer,
    Number,
    ProfileMapping,
    ReadJsonObject,
    RequireExactKeys,
    RequireMapping,
)
from trader.recommendation.domain.scoring.profile_identity import ScoringProfileId
from trader.training.infra.model_fitting import (
    LightGBMFittingParameters,
    ModelFittingParameters,
    RidgeFittingParameters,
)

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
            or self.tomorrow_feature_ids != profile.heads[0].feature_manifest.names
            or self.d25_feature_ids != profile.heads[1].feature_manifest.names
        ):
            raise ValueError("training input settings do not match the profile feature contract")


def LoadProfileTrainingSettings(project_root: Path, profile_id: ScoringProfileId) -> ProfileTrainingSettings:
    document = ReadJsonObject(project_root / "config" / "training.json")
    RequireExactKeys(document, {"profiles"}, "training")
    profile = ProfileMapping(document.get("profiles"), profile_id, "training.profiles")
    common_keys = {"fit_dates", "minimum_industry_training_rows"}
    if profile_id == "v2":
        RequireExactKeys(profile, common_keys | {"lightgbm"}, "training.v2")
        model = RequireMapping(profile, "lightgbm")
        RequireExactKeys(
            model,
            {
                "learning_rate",
                "max_depth",
                "num_leaves",
                "minimum_leaf_rows",
                "boosting_rounds",
                "early_stopping_rounds",
                "maximum_bins",
                "histogram_pool_mib",
                "seed",
            },
            "training.v2.lightgbm",
        )
        fitting: ModelFittingParameters = LightGBMFittingParameters(
            Integer(profile, "minimum_industry_training_rows", minimum=1),
            Number(model, "learning_rate", minimum=0.000001),
            Integer(model, "max_depth", minimum=1),
            Integer(model, "num_leaves", minimum=2),
            Integer(model, "minimum_leaf_rows", minimum=1),
            Integer(model, "boosting_rounds", minimum=1),
            Integer(model, "early_stopping_rounds", minimum=1),
            Integer(model, "maximum_bins", minimum=2),
            Integer(model, "histogram_pool_mib", minimum=1),
            Integer(model, "seed", minimum=0),
        )
    else:
        RequireExactKeys(profile, common_keys | {"ridge"}, "training.v3")
        model = RequireMapping(profile, "ridge")
        RequireExactKeys(model, {"penalty"}, "training.v3.ridge")
        fitting = RidgeFittingParameters(
            Integer(profile, "minimum_industry_training_rows", minimum=1),
            Number(model, "penalty", minimum=0.000001),
        )
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
        Integer(profile, "fit_dates", minimum=1),
        Integer(inputs, "history_sessions", minimum=61),
        _positive_integer_tuple(
            inputs.get("momentum_horizons"), f"features.model_contracts.{profile_id}.momentum_horizons"
        ),
        _positive_integer_tuple(
            inputs.get("market_state_momentum_horizons"),
            f"features.model_contracts.{profile_id}.market_state_momentum_horizons",
            allow_empty=True,
        ),
        _feature_ids(heads.get("tomorrow"), f"features.model_contracts.{profile_id}.heads.tomorrow"),
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


__all__ = ["ProfileTrainingSettings", "LoadProfileTrainingSettings"]
