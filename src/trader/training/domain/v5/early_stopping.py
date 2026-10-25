"""Significant validation improvements control patience independently of best weights."""

import math
from dataclasses import dataclass


@dataclass
class TcnEarlyStopping:
    relative_delta: float
    best: float = math.inf
    significant_best: float = math.inf
    stale: int = 0

    def __post_init__(self) -> None:
        if not math.isfinite(self.relative_delta) or not 0 <= self.relative_delta < 1:
            raise ValueError("V5 early stopping relative delta is invalid")

    def observe(self, metric: float) -> bool:
        if not math.isfinite(metric) or metric < 0:
            raise ValueError("V5 nonfinite or negative early stopping loss")
        improved = metric < self.best
        self.best = min(self.best, metric)
        if math.isinf(self.significant_best) or (
            metric < self.significant_best
            and (self.significant_best - metric) / max(abs(self.significant_best), 1e-12) >= self.relative_delta
        ):
            self.significant_best = metric
            self.stale = 0
        else:
            self.stale += 1
        return improved
