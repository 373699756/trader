"""Shared profile composition primitives."""

from __future__ import annotations

from collections.abc import Sequence

from trader.application.ports.model_scoring import HeadPrediction


class SingleHeadCombiner:
    """Return the only head signal in the current production profiles."""

    def combine(self, predictions: Sequence[HeadPrediction]) -> HeadPrediction:
        if len(predictions) != 1:
            raise ValueError("single-head scoring profile requires exactly one prediction")
        return predictions[0]


__all__ = ["SingleHeadCombiner"]
