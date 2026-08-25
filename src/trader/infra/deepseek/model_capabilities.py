"""Known DeepSeek model capabilities registry.

This module is the single source of truth for model capability declarations.
Adding a new model requires:
1. Add an entry to ``MODELS`` below.
2. Ensure it is listed in ``model_catalog.py``.
3. Run the capability contract tests.
"""

from __future__ import annotations

from trader.infra.deepseek.base_client import ModelCapabilities

MODELS: dict[str, ModelCapabilities] = {
    "deepseek-v4-flash": ModelCapabilities(
        preferred_structured_method="json_object",
        requires_reasoning_roundtrip=False,
        supports_tool_choice=False,
        reasoning_effort=None,
    ),
    "deepseek-v4-pro": ModelCapabilities(
        preferred_structured_method="json_object",
        requires_reasoning_roundtrip=True,
        supports_tool_choice=False,
        reasoning_effort="high",
    ),
}

_UNKNOWN_MODEL_CAPABILITIES = ModelCapabilities(
    preferred_structured_method="json_object",
    requires_reasoning_roundtrip=False,
    supports_tool_choice=False,
    reasoning_effort=None,
)


def capabilities(model: str) -> ModelCapabilities:
    """Look up capabilities, conservatively defaulting unknown models."""
    return MODELS.get(model, _UNKNOWN_MODEL_CAPABILITIES)


__all__ = ["MODELS", "capabilities"]
