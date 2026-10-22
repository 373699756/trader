"""Parse the independent learned-rank training configuration at the I/O boundary."""

from collections.abc import Mapping
from pathlib import Path

from trader.infra.settings.parser import Integer, Number, ProfileMapping, ReadJsonObject, RequireExactKeys
from trader.training.domain.v1.learned_rank import V1TrainingSettings


def LoadV1TrainingSettings(project_root: Path) -> V1TrainingSettings:
    document = ReadJsonObject(project_root / "config" / "training.json")
    RequireExactKeys(document, {"profiles"}, "training")
    values = ProfileMapping(document.get("profiles"), "v1", "training.profiles")
    return ParseV1TrainingSettings(values)


def ParseV1TrainingSettings(values: Mapping[str, object]) -> V1TrainingSettings:
    """Parse the same strict settings in config and the artifact manifest."""
    integer_fields = (
        "fit_dates",
        "early_stopping_dates",
        "calibration_dates",
        "validation_dates",
        "embargo_dates",
        "minimum_training_rows",
        "max_depth",
        "num_leaves",
        "minimum_leaf_rows",
        "boosting_rounds",
        "early_stopping_rounds",
        "maximum_bins",
        "histogram_pool_mib",
        "seed",
    )
    RequireExactKeys(
        values, {*integer_fields, "learning_rate", "ridge_penalty", "ranking_round_trip_cost"}, "training.v1"
    )
    return V1TrainingSettings(
        fit_dates=Integer(values, "fit_dates", minimum=1),
        early_stopping_dates=Integer(values, "early_stopping_dates", minimum=1),
        calibration_dates=Integer(values, "calibration_dates", minimum=1),
        validation_dates=Integer(values, "validation_dates", minimum=1),
        embargo_dates=Integer(values, "embargo_dates", minimum=5),
        minimum_training_rows=Integer(values, "minimum_training_rows", minimum=1),
        learning_rate=Number(values, "learning_rate", minimum=0.000001),
        max_depth=Integer(values, "max_depth", minimum=1),
        num_leaves=Integer(values, "num_leaves", minimum=2),
        minimum_leaf_rows=Integer(values, "minimum_leaf_rows", minimum=1),
        boosting_rounds=Integer(values, "boosting_rounds", minimum=1),
        early_stopping_rounds=Integer(values, "early_stopping_rounds", minimum=1),
        maximum_bins=Integer(values, "maximum_bins", minimum=2),
        histogram_pool_mib=Integer(values, "histogram_pool_mib", minimum=1),
        seed=Integer(values, "seed", minimum=0),
        ridge_penalty=Number(values, "ridge_penalty", minimum=0.000001),
        ranking_round_trip_cost=Number(values, "ranking_round_trip_cost", minimum=0.0),
    )


__all__ = ["LoadV1TrainingSettings", "ParseV1TrainingSettings"]
