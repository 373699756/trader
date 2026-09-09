"""Typed prediction signal without risk, cost, review, or action semantics."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from trader.domain.recommendation.model_scoring.profile_identity import ScoringProfileId

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_IDENTITY = re.compile(r"^[a-z0-9_]{1,96}$")


@dataclass(frozen=True)
class AlphaScore:
    code: str
    profile_id: ScoringProfileId
    model_id: str
    model_hash: str
    feature_vector_hash: str
    signal_score: float
    predicted_excess_return: float

    def __post_init__(self) -> None:
        if len(self.code) != 6 or not self.code.isdigit():
            raise ValueError("alpha score code must contain exactly six digits")
        if self.profile_id not in {"v1", "v2", "v3"} or _IDENTITY.fullmatch(self.model_id) is None:
            raise ValueError("alpha score model identity is invalid")
        if _SHA256.fullmatch(self.model_hash) is None or _SHA256.fullmatch(self.feature_vector_hash) is None:
            raise ValueError("alpha score content identity is invalid")
        if (
            not math.isfinite(self.signal_score)
            or not 0.0 <= self.signal_score <= 100.0
            or not math.isfinite(self.predicted_excess_return)
        ):
            raise ValueError("alpha score values are invalid")


__all__ = ["AlphaScore"]
