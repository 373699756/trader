"""Immutable V5 fitting parameters, independent of the optional tensor runtime."""

from dataclasses import dataclass


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

    def __post_init__(self) -> None:
        if (
            self.minimum_industry_training_rows < 1
            or not 0 < self.learning_rate <= 1
            or not 0 <= self.weight_decay <= 1
            or not 0 <= self.dropout < 1
            or not 1 <= self.batch_size <= 256
            or not 1 <= self.epochs <= 100
            or not 1 <= self.early_stopping_rounds <= self.epochs
            or self.seed < 0
        ):
            raise ValueError("V5 fitting parameters are outside their bounded contract")


__all__ = ["TcnFittingParameters"]
