from __future__ import annotations

from typing import Dict


class DeepSeekRuntimeConfig:
    """Thin object boundary around DeepSeek runtime configuration loading."""

    def __init__(self, loader) -> None:
        self._loader = loader

    def load(self) -> Dict[str, object]:
        return self._loader()
