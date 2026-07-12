from __future__ import annotations

from importlib import import_module


_MODULE_EXPORTS = {
    "candidate_filters",
    "explanations",
    "market_regime",
    "scoring_math",
    "theme_limits",
    "today_score",
    "tomorrow_score",
    "tomorrow_policy",
}

_CLASS_EXPORTS = {
    "FeatureBuilder": (".features", "FeatureBuilder"),
    "ExplanationBuilder": (".policies", "ExplanationBuilder"),
    "RankingPolicy": (".policies", "RankingPolicy"),
    "RiskPolicy": (".policies", "RiskPolicy"),
}


def __getattr__(name: str):
    if name in _MODULE_EXPORTS:
        module = import_module("{}.{}".format(__name__, name))
        globals()[name] = module
        return module
    target = _CLASS_EXPORTS.get(name)
    if target is not None:
        module_name, attr_name = target
        value = getattr(import_module(module_name, __name__), attr_name)
        globals()[name] = value
        return value
    raise AttributeError("module {!r} has no attribute {!r}".format(__name__, name))

__all__ = [
    "ExplanationBuilder",
    "FeatureBuilder",
    "RankingPolicy",
    "RiskPolicy",
    "candidate_filters",
    "explanations",
    "market_regime",
    "scoring_math",
    "theme_limits",
    "today_score",
    "tomorrow_score",
    "tomorrow_policy",
]
