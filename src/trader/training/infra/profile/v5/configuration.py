"""Strict configuration parsing and whitelisted portable training snapshots."""

from collections.abc import Mapping
from dataclasses import asdict
from typing import cast

from trader.infra.serialization.canonical import ContentHash
from trader.infra.settings.parser import Integer, Number, RequireExactKeys
from trader.training.infra.profile.v5.settings import TcnComputeDtype, TcnFittingParameters


def ParseTcnParameters(model: Mapping[str, object], minimum_rows: int) -> TcnFittingParameters:
    RequireExactKeys(
        model,
        {
            "learning_rate",
            "weight_decay",
            "dropout",
            "batch_size",
            "epochs",
            "early_stopping_rounds",
            "seed",
            "hidden_channels",
            "compute_dtype",
            "early_stopping_relative_delta",
            "compute_threads",
        },
        "training.v5.tcn",
    )
    dtype = model["compute_dtype"]
    if not isinstance(dtype, str) or dtype not in ("float32", "float64"):
        raise ValueError("training.v5.tcn.compute_dtype must be float32 or float64")
    return TcnFittingParameters(
        minimum_rows,
        Number(model, "learning_rate", minimum=0.000001),
        Number(model, "weight_decay", minimum=0),
        Number(model, "dropout", minimum=0),
        Integer(model, "batch_size", minimum=1),
        Integer(model, "epochs", minimum=1),
        Integer(model, "early_stopping_rounds", minimum=1),
        Integer(model, "seed", minimum=0),
        Integer(model, "hidden_channels", minimum=1),
        cast(TcnComputeDtype, dtype),
        Number(model, "early_stopping_relative_delta", minimum=0),
        Integer(model, "compute_threads", minimum=1),
    )


def TcnConfigurationDocument(fit_dates: int, parameters: TcnFittingParameters) -> dict[str, object]:
    document: dict[str, object] = {"fit_dates": fit_dates, "parameters": asdict(parameters)}
    return {**document, "content_hash": ContentHash(document)}


def DecodeTcnConfiguration(value: object) -> tuple[int, TcnFittingParameters]:
    if not isinstance(value, dict) or set(value) != {"fit_dates", "parameters", "content_hash"}:
        raise ValueError("V5 training configuration snapshot is invalid")
    fit_dates = Integer(value, "fit_dates", minimum=250)
    raw = value["parameters"]
    if not isinstance(raw, dict) or any(not isinstance(key, str) for key in raw):
        raise ValueError("V5 training parameters snapshot is invalid")
    fields = dict(raw)
    minimum = Integer(fields, "minimum_industry_training_rows", minimum=1)
    del fields["minimum_industry_training_rows"]
    parameters = ParseTcnParameters(fields, minimum)
    if value != TcnConfigurationDocument(fit_dates, parameters):
        raise ValueError("V5 training configuration snapshot hash is invalid")
    return fit_dates, parameters
