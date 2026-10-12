"""Best-effort initialization for optional local persistence observers."""

from __future__ import annotations

import sqlite3
from typing import Protocol


class Initializable(Protocol):
    def initialize(self) -> None: ...


def initialize_if_available(resource: Initializable) -> bool:
    try:
        resource.initialize()
    except (OSError, sqlite3.Error):
        return False
    return True


__all__ = ["Initializable", "initialize_if_available"]
