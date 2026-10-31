"""Immutable SQLite I/O policy selected at the composition boundary."""

from dataclasses import dataclass
from math import isfinite


@dataclass(frozen=True)
class SqliteIoPolicy:
    connection_timeout_seconds: float
    read_window_rows: int
    transaction_batch_rows: int
    checkpoint_mode: str

    def __post_init__(self) -> None:
        if (
            isinstance(self.connection_timeout_seconds, bool)
            or not isinstance(self.connection_timeout_seconds, (int, float))
            or not isfinite(self.connection_timeout_seconds)
            or self.connection_timeout_seconds <= 0
            or type(self.read_window_rows) is not int
            or self.read_window_rows < 1
            or type(self.transaction_batch_rows) is not int
            or self.transaction_batch_rows < 1
            or self.checkpoint_mode not in {"PASSIVE", "RESTART", "TRUNCATE"}
        ):
            raise ValueError("storage medium settings are invalid")
