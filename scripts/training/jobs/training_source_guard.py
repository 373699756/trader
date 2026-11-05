"""Keep diagnostic fitting on published history read-only, including due projections."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
import sqlite3
from typing import Literal, TYPE_CHECKING
from unittest.mock import patch
from urllib.parse import parse_qs, unquote, urlsplit

if TYPE_CHECKING:
    from trader.infra.sqlite_policy import SqliteIoPolicy
    from trader.training.infra.history.history_training_due import HistoryTrainingDueEvaluation, HistoryTrainingDueQuery


@contextmanager
def ReadOnlyTrainingSource(history: Path) -> Iterator[None]:
    """Reject writable source opens and calculate reminders without persisting them."""
    from trader.training.infra.history import history_training_due

    source_root = history.resolve().parent
    original_connect = sqlite3.connect
    original_due = history_training_due.EvaluateHistoryTrainingDue

    def connect(
        database: str | Path,
        timeout: float = 5.0,
        detect_types: int = 0,
        isolation_level: Literal["DEFERRED", "EXCLUSIVE", "IMMEDIATE", ""] | None = "",
        check_same_thread: bool = True,
        factory: type[sqlite3.Connection] = sqlite3.Connection,
        cached_statements: int = 128,
        uri: bool = False,
    ) -> sqlite3.Connection:
        document = urlsplit(str(database)) if uri else None
        path = Path(unquote(document.path) if document is not None else database).resolve()
        if path.is_relative_to(source_root) and (
            document is None or parse_qs(document.query).get("mode") != ["ro"]
        ):
            raise ValueError("training_published_source_requires_read_only_connection")
        return original_connect(
            database,
            timeout=timeout,
            detect_types=detect_types,
            isolation_level="DEFERRED" if isolation_level == "" else isolation_level,
            check_same_thread=check_same_thread,
            factory=factory,
            cached_statements=cached_statements,
            uri=uri,
        )

    def due(
        query: HistoryTrainingDueQuery, *, sqlite_policy: SqliteIoPolicy
    ) -> HistoryTrainingDueEvaluation | None:
        return original_due(replace(query, persist=False), sqlite_policy=sqlite_policy)

    with (
        patch.object(sqlite3, "connect", connect),
        patch.object(history_training_due, "EvaluateHistoryTrainingDue", due),
    ):
        yield


__all__ = ["ReadOnlyTrainingSource"]
