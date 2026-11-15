"""Translate driver failures before they enter the synchronization use case."""

import sqlite3
from collections.abc import Callable
from typing import ParamSpec, TypeVar

from trader.download.application.sync_data import SyncDatabaseError

_Parameters = ParamSpec("_Parameters")
_Result = TypeVar("_Result")


def AdaptSyncDatabaseFailures(operation: Callable[_Parameters, _Result]) -> Callable[_Parameters, _Result]:
    def execute(*args: _Parameters.args, **kwargs: _Parameters.kwargs) -> _Result:
        try:
            return operation(*args, **kwargs)
        except sqlite3.Error as exc:
            raise SyncDatabaseError(type(exc).__name__) from exc

    return execute
