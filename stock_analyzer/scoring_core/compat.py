from __future__ import annotations

import inspect
from types import MappingProxyType
from typing import Callable, Dict, Iterable, List, Mapping, Tuple

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
    return MappingProxyType({
        name: value
        for name, value in namespace.items()
        if not name.startswith("__")
    })


def _merge_scoring_context(
    namespace: Dict[str, object],
    baseline: Mapping[str, object],
    strategy_entrypoints: Iterable[str],
    baseline_context: Mapping[str, object] = None,
) -> Dict[str, object]:
    runtime_context = collect_scoring_context(namespace, baseline, strategy_entrypoints)
    if not baseline_context:
        return dict(runtime_context)
    merged = dict(runtime_context)
    merged.update(dict(baseline_context))
    return merged


def collect_module_overrides(
    namespace: Dict[str, object],
    baseline: Dict[str, object],
    module,
    strategy_entrypoints: Iterable[str] = (),
) -> List[Tuple[str, object, object]]:
    entrypoints = set(strategy_entrypoints)
    overrides = []
    for name, value in list(namespace.items()):
        if name.startswith("__") or name in entrypoints:
            continue
        if baseline.get(name) is value:
            continue
        if hasattr(module, name) and getattr(module, name) is not value:
            overrides.append((name, getattr(module, name), value))
    return overrides


def collect_scoring_core_overrides(
    namespace: Dict[str, object],
    baseline: Dict[str, object],
    strategy_entrypoints: Iterable[str],
) -> List[Tuple[str, object, object]]:
    return [
        override
        for module in _OVERRIDE_MODULES
        for override in collect_module_overrides(namespace, baseline, module, strategy_entrypoints)
        if override
    ]


def collect_scoring_context(
    namespace: Dict[str, object],
    baseline: Dict[str, object],
    strategy_entrypoints: Iterable[str],
) -> Mapping[str, object]:
    overrides = collect_scoring_core_overrides(namespace, baseline, strategy_entrypoints)
    if not overrides:
        return MappingProxyType({})
    values = dict((name, value) for name, _old_value, value in overrides)
    return MappingProxyType(values)


def call_with_scoring_core_overrides(
    namespace: Dict[str, object],
    baseline: Dict[str, object],
    strategy_entrypoints: Iterable[str],
    callback: Callable[[], object],
    scoring_context: Dict[str, object] = None,
):
    scoring_context = _merge_scoring_context(
        namespace,
        baseline,
        strategy_entrypoints,
        baseline_context=scoring_context,
    )
    immutable_context = MappingProxyType(scoring_context)
    if not callable(callback):
        raise TypeError("callback must be callable")
    signature = inspect.signature(callback)
    if "scoring_context" in signature.parameters:
        return callback(scoring_context=immutable_context)
    return callback()


def call_legacy_strategy(
    strategy_name: str,
    namespace: Dict[str, object],
    baseline: Dict[str, object],
    strategy_entrypoints: Iterable[str],
    scoring_context: Dict[str, object] = None,
    *args,
    **kwargs,
):
    scoring_context = MappingProxyType(
        _merge_scoring_context(
            namespace,
            baseline,
            strategy_entrypoints,
            baseline_context=scoring_context,
        )
    )

    def callback():
        if strategy_name == "today":
            from ..strategies.today import TodayScorer

            return TodayScorer(scoring_context=scoring_context).score(*args, **kwargs)
        if strategy_name == "tomorrow":
            from ..strategies.tomorrow import TomorrowScorer

            return TomorrowScorer(scoring_context=scoring_context).score(*args, **kwargs)
        if strategy_name == "swing":
            from ..strategies.swing_2_5d import SwingScorer

            return SwingScorer(scoring_context=scoring_context).score(*args, **kwargs)
        raise KeyError("unknown legacy strategy: {}".format(strategy_name))

    return callback()


__all__ = [
    "call_legacy_strategy",
    "call_with_scoring_core_overrides",
    "collect_scoring_context",
    "collect_module_overrides",
    "collect_scoring_core_overrides",
    "install_legacy_exports",
]
