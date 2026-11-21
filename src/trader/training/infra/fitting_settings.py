"""Read the native fitting settings shared by both strategy trainers."""

from dataclasses import asdict, dataclass
from pathlib import Path

from trader.infra.settings.parser import Integer, Number, ProfileMapping, RequireExactKeys, RequireMapping
from trader.recommendation.domain.scoring.profile_identity import ScoringProfileId
from trader.training.infra.resource_settings import ReadTrainingConfiguration
from trader.training.infra.tcn.configuration import ParseTcnParameters
from trader.training.infra.tcn.settings import TcnFittingParameters


@dataclass(frozen=True)
class LightGBMFittingParameters:
    minimum_industry_training_rows: int
    learning_rate: float
    max_depth: int
    num_leaves: int
    minimum_leaf_rows: int
    boosting_rounds: int
    early_stopping_rounds: int
    maximum_bins: int
    histogram_pool_mib: int
    seed: int


@dataclass(frozen=True)
class RidgeFittingParameters:
    minimum_industry_training_rows: int
    penalty: float


ModelFittingParameters = LightGBMFittingParameters | RidgeFittingParameters | TcnFittingParameters


def FittingParameterDocument(parameters: ModelFittingParameters) -> dict[str, object]:
    """Project fitting identity at the persisted contract boundary."""
    return asdict(parameters)


def LoadIndustryFittingSettings(
    project_root: Path, profile_id: ScoringProfileId,
) -> tuple[int, ModelFittingParameters]:
    if profile_id not in {"v2", "v3", "v5"}:
        raise ValueError("fitting settings require v2, v3 or v5; V1 owns learned-rank settings")
    document = ReadTrainingConfiguration(project_root / "config" / "training.json")
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
    elif profile_id == "v5":
        RequireExactKeys(profile, common_keys | {"tcn"}, "training.v5")
        model = RequireMapping(profile, "tcn")
        fitting = ParseTcnParameters(model, Integer(profile, "minimum_industry_training_rows", minimum=1))
    else:
        RequireExactKeys(profile, common_keys | {"ridge"}, "training.v3")
        model = RequireMapping(profile, "ridge")
        RequireExactKeys(model, {"penalty"}, "training.v3.ridge")
        fitting = RidgeFittingParameters(
            Integer(profile, "minimum_industry_training_rows", minimum=1),
            Number(model, "penalty", minimum=0.000001),
        )
    return Integer(profile, "fit_dates", minimum=61 if profile_id == "v5" else 1), fitting
