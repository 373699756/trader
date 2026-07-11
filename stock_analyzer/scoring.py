"""Compatibility facade for scoring APIs.

Concrete strategy workflows live in ``stock_analyzer.strategies`` and shared
building blocks live in ``stock_analyzer.scoring_core``. This module keeps the
legacy import surface stable for callers and tests.
"""

from __future__ import annotations

from .scoring_core import base as _base
from .strategies.swing_2_5d import SwingScorer
from .strategies.today import TodayScorer
from .strategies.tomorrow import TomorrowScorer


for _name in dir(_base):
    if _name.startswith("__"):
        continue
    globals()[_name] = getattr(_base, _name)


_STRATEGY_ENTRYPOINTS = {
    "score_today_candidates",
    "score_tomorrow_candidates",
    "score_swing_candidates",
}


def _collect_base_overrides():
    """Collect monkeypatches made on the legacy facade for scoring_core."""
    overrides = []
    for name, value in list(globals().items()):
        if name.startswith("__") or name in _STRATEGY_ENTRYPOINTS:
            continue
        if hasattr(_base, name) and getattr(_base, name) is not value:
            overrides.append((name, getattr(_base, name), value))
    return overrides


def _call_with_base_overrides(callback):
    overrides = _collect_base_overrides()
    for name, _old_value, new_value in overrides:
        setattr(_base, name, new_value)
    try:
        return callback()
    finally:
        for name, old_value, _new_value in reversed(overrides):
            setattr(_base, name, old_value)


def score_today_candidates(*args, **kwargs):
    return _call_with_base_overrides(lambda: TodayScorer().score(*args, **kwargs))


def score_tomorrow_candidates(*args, **kwargs):
    return _call_with_base_overrides(lambda: TomorrowScorer().score(*args, **kwargs))


def score_swing_candidates(*args, **kwargs):
    return _call_with_base_overrides(lambda: SwingScorer().score(*args, **kwargs))


__all__ = [
    name
    for name in globals()
    if not name.startswith("__") and name not in {"_base", "_name"}
]
