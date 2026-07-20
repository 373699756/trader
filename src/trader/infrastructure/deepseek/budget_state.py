"""Typed shared state contract for DeepSeek budget mixins."""

from __future__ import annotations

import sqlite3
from contextlib import AbstractContextManager


class BudgetStoreState:
    _initialized: bool
    _daily_hard_limit: int
    _daily_target: int
    _limits: dict[str, int]
    _stage_targets: dict[str, int]
    _stage_limits: dict[str, int]
    _challenger_limits: dict[str, int]

    def _connect(self) -> AbstractContextManager[sqlite3.Connection]:
        raise NotImplementedError
