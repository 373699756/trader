"""Known DeepSeek model catalog with validation.

This module validates that the configured model is known.
Unknown models generate a ``RuntimeWarning`` but do not block execution.
"""

from __future__ import annotations

import warnings

from trader.infra.deepseek.model_capabilities import MODELS, is_decommissioned

KNOWN_MODELS: frozenset[str] = frozenset(MODELS.keys())


def validate_model(model: str, *, strict: bool = False) -> None:
    """Validate *model* against the known model catalog.

    Args:
        model: The model identifier to validate.
        strict: If ``True``, raise ``ValueError`` for unknown or decommissioned
            models instead of issuing a warning.

    Raises:
        ValueError: When *strict* is ``True`` and the model is unknown or
            decommissioned.
    """
    if model not in KNOWN_MODELS:
        message = f"Unknown DeepSeek model '{model}'; known models: {sorted(KNOWN_MODELS)}"
        if strict:
            raise ValueError(message)
        warnings.warn(message, RuntimeWarning, stacklevel=2)
        return
    if is_decommissioned(model):
        message = (
            f"DeepSeek model '{model}' is scheduled for decommission on 2026-07-24. "
            f"Switch to deepseek-v4-flash or deepseek-v4-pro. "
            f"See https://api-docs.deepseek.com/news/news260424/"
        )
        if strict:
            raise ValueError(message)
        warnings.warn(message, FutureWarning, stacklevel=2)


__all__ = ["KNOWN_MODELS", "validate_model"]
