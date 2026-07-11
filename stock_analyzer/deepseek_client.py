"""Compatibility facade for DeepSeek integration.

Implementation lives under :mod:`stock_analyzer.deepseek`.
"""

from __future__ import annotations

from .deepseek import service as _service

for _name in dir(_service):
    if not _name.startswith("__"):
        globals()[_name] = getattr(_service, _name)

requests = _service.requests


def _sync_patchable_hooks() -> None:
    _service._load_dotenv_if_needed = globals().get("_load_dotenv_if_needed", _service._load_dotenv_if_needed)


def _coerce_env_config():
    _sync_patchable_hooks()
    return _service._coerce_env_config()


def rerank_candidates(*args, **kwargs):
    _sync_patchable_hooks()
    return _service.rerank_candidates(*args, **kwargs)


def rerank_candidates_batch(*args, **kwargs):
    _sync_patchable_hooks()
    return _service.rerank_candidates_batch(*args, **kwargs)


def review_market_regime(*args, **kwargs):
    _sync_patchable_hooks()
    return _service.review_market_regime(*args, **kwargs)


def review_strategy_validation(*args, **kwargs):
    _sync_patchable_hooks()
    return _service.review_strategy_validation(*args, **kwargs)


__all__ = [name for name in globals() if not name.startswith("__")]
