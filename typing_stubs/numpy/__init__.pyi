"""Opaque NumPy boundary for cross-version project type checking."""

from typing import Any

def __getattr__(name: str) -> Any: ...
