from __future__ import annotations

from typing import Callable, Dict, Iterable, List, Tuple

from . import base as _base
from . import explanations as _explanations
from . import expected_return as _expected_return
from . import risk as _risk
from . import scoring_math as _scoring_math
from . import today_score as _today_score
from . import tomorrow_policy as _tomorrow_policy
from . import tomorrow_score as _tomorrow_score


_EXPORT_MODULES = (
    _base,
    _scoring_math,
    _risk,
    _expected_return,
    _today_score,
    _tomorrow_score,
    _tomorrow_policy,
    _explanations,
)

_OVERRIDE_MODULES = _EXPORT_MODULES


def install_legacy_exports(namespace: Dict[str, object]) -> Dict[str, object]:
    for module in _EXPORT_MODULES:
        for name in getattr(module, "__all__", ()):
            namespace[name] = getattr(module, name)
    return {
        name: value
        for name, value in namespace.items()
        if not name.startswith("__")
    }


def collect_module_overrides(
    namespace: Dict[str, object],
    baseline: Dict[str, object],
    module,
    strategy_entrypoints: Iterable[str],
) -> List[Tuple[object, str, object, object]]:
    entrypoints = set(strategy_entrypoints)
    overrides = []
    for name, value in list(namespace.items()):
        if name.startswith("__") or name in entrypoints:
            continue
        if baseline.get(name) is value:
            continue
        if hasattr(module, name) and getattr(module, name) is not value:
            overrides.append((module, name, getattr(module, name), value))
    return overrides


def collect_scoring_core_overrides(
    namespace: Dict[str, object],
    baseline: Dict[str, object],
    strategy_entrypoints: Iterable[str],
) -> List[Tuple[object, str, object, object]]:
    return [
        override
        for module in _OVERRIDE_MODULES
        for override in collect_module_overrides(namespace, baseline, module, strategy_entrypoints)
    ]


def call_with_scoring_core_overrides(
    namespace: Dict[str, object],
    baseline: Dict[str, object],
    strategy_entrypoints: Iterable[str],
    callback: Callable[[], object],
):
    overrides = collect_scoring_core_overrides(namespace, baseline, strategy_entrypoints)
    for module, name, _old_value, new_value in overrides:
        setattr(module, name, new_value)
    try:
        return callback()
    finally:
        for module, name, old_value, _new_value in reversed(overrides):
            setattr(module, name, old_value)


def call_legacy_strategy(
    strategy_name: str,
    namespace: Dict[str, object],
    baseline: Dict[str, object],
    strategy_entrypoints: Iterable[str],
    *args,
    **kwargs,
):
    def callback():
        if strategy_name == "today":
            from ..strategies.today import TodayScorer

            return TodayScorer().score(*args, **kwargs)
        if strategy_name == "tomorrow":
            from ..strategies.tomorrow import TomorrowScorer

            return TomorrowScorer().score(*args, **kwargs)
        if strategy_name == "swing":
            from ..strategies.swing_2_5d import SwingScorer

            return SwingScorer().score(*args, **kwargs)
        raise KeyError("unknown legacy strategy: {}".format(strategy_name))

    return call_with_scoring_core_overrides(namespace, baseline, strategy_entrypoints, callback)


__all__ = [
    "call_legacy_strategy",
    "call_with_scoring_core_overrides",
    "collect_module_overrides",
    "collect_scoring_core_overrides",
    "install_legacy_exports",
]
