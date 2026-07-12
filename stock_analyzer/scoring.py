"""Compatibility facade for scoring APIs.

Concrete strategy workflows live in ``stock_analyzer.strategies`` and shared
building blocks live in ``stock_analyzer.scoring_core``. This module keeps the
legacy import surface stable for callers and tests.
"""

from __future__ import annotations

from .scoring_core.compat import call_legacy_strategy, install_legacy_exports


_FACADE_BASELINE = install_legacy_exports(globals())


_STRATEGY_ENTRYPOINTS = {
    "score_today_candidates",
    "score_tomorrow_candidates",
    "score_swing_candidates",
}


def score_today_candidates(*args, **kwargs):
    return call_legacy_strategy("today", globals(), _FACADE_BASELINE, _STRATEGY_ENTRYPOINTS, *args, **kwargs)


def score_tomorrow_candidates(*args, **kwargs):
    return call_legacy_strategy("tomorrow", globals(), _FACADE_BASELINE, _STRATEGY_ENTRYPOINTS, *args, **kwargs)


def score_swing_candidates(*args, **kwargs):
    return call_legacy_strategy("swing", globals(), _FACADE_BASELINE, _STRATEGY_ENTRYPOINTS, *args, **kwargs)


__all__ = [
    name
    for name in globals()
    if not name.startswith("__")
    and name
    not in {
        "_FACADE_BASELINE",
        "_STRATEGY_ENTRYPOINTS",
        "call_legacy_strategy",
        "install_legacy_exports",
        "_name",
    }
]
