"""Immutable V5 fitting parameters, independent of the optional tensor runtime."""

import math
from dataclasses import dataclass
from typing import Literal

TcnComputeDtype = Literal["float32", "float64"]
TCN_NETWORK_CONTRACT = "causal_tcn_blocks4_kernel3_dilations1_2_4_8_v1"


@dataclass(frozen=True)
class TcnArchitecture:
    hidden_channels: int
    compute_dtype: TcnComputeDtype

    def __post_init__(self) -> None:
        if type(self.hidden_channels) is not int or self.hidden_channels not in (4, 8, 16):
            raise ValueError("V5 hidden_channels must be 4, 8 or 16")
        if self.compute_dtype not in ("float32", "float64"):
            raise ValueError("V5 compute_dtype must be float32 or float64")


@dataclass(frozen=True)
class TcnFittingParameters:
    minimum_industry_training_rows: int
    learning_rate: float = 0.001
    weight_decay: float = 0.0001
    dropout: float = 0.1
    batch_size: int = 256
    epochs: int = 100
    early_stopping_rounds: int = 10
    seed: int = 0
    hidden_channels: int = 16
    compute_dtype: TcnComputeDtype = "float64"
    early_stopping_relative_delta: float = 0.0
    compute_threads: int = 2

    def __post_init__(self) -> None:
        TcnArchitecture(self.hidden_channels, self.compute_dtype)
        if (
            any(
                type(value) is not int
                for value in (
                    self.minimum_industry_training_rows,
                    self.batch_size,
                    self.epochs,
                    self.early_stopping_rounds,
                    self.seed,
                    self.compute_threads,
                )
            )
            or not all(
                math.isfinite(value)
                for value in (
                    self.learning_rate,
                    self.weight_decay,
                    self.dropout,
                    self.early_stopping_relative_delta,
                )
            )
            or self.minimum_industry_training_rows < 1
            or not 0 < self.learning_rate <= 1
            or not 0 <= self.weight_decay <= 1
            or not 0 <= self.dropout < 1
            or not 1 <= self.batch_size <= 256
            or not 1 <= self.epochs <= 100
            or not 1 <= self.early_stopping_rounds <= self.epochs
            or self.seed < 0
            or not 0 <= self.early_stopping_relative_delta < 1
            or not 1 <= self.compute_threads <= 4
        ):
            raise ValueError("V5 fitting parameters are outside their bounded contract")

    @property
    def architecture(self) -> TcnArchitecture:
        return TcnArchitecture(self.hidden_channels, self.compute_dtype)


__all__ = ["TCN_NETWORK_CONTRACT", "TcnArchitecture", "TcnComputeDtype", "TcnFittingParameters"]
