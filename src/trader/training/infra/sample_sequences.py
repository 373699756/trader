"""Lossless overlap encoding and bounded batch reads for sequence sample features."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass, field

import numpy as np

from trader.infra.sqlite_connection import SqliteConnection

SEQUENCE_STORAGE_CONTRACT = "overlapping_sequence_blocks_float64_le_v1"
_BLOCK_STEPS = 64
_WRITER_CODES = 8192
_READER_BYTES = 32 * 1024 * 1024


@dataclass(frozen=True)
class SampleSequenceLayout:
    auxiliary: int
    steps: int
    channels: int

    def __post_init__(self) -> None:
        if not 0 < self.auxiliary <= 64 or not 0 < self.steps <= _BLOCK_STEPS or not 0 < self.channels <= 16:
            raise ValueError("sample sequence layout exceeds bounded capacity")


@dataclass
class _SequenceTail:
    next_position: int
    previous: bytes = b""
    pending: bytearray = field(default_factory=bytearray)


class SampleSequenceWriter:
    """Share exact overlapping values; evicting a code only loses a compression opportunity."""

    def __init__(self, connection: SqliteConnection, layout: SampleSequenceLayout) -> None:
        self._connection = connection
        self._layout = layout
        self._tails: OrderedDict[str, _SequenceTail] = OrderedDict()

    def append(self, code: str, features: tuple[float, ...]) -> int:
        values = np.asarray(features, dtype="<f8")
        if values.size != self._layout.steps * self._layout.channels or not np.isfinite(values).all():
            raise ValueError("sample sequence dimensions or values are invalid")
        payload = values.tobytes()
        tail = self._tails.pop(code, None)
        if tail is None:
            if len(self._tails) >= _WRITER_CODES:
                previous_code, previous = self._tails.popitem(last=False)
                self._flush(previous_code, previous)
            row = self._connection.run(
                "SELECT MAX(block) FROM sample_sequence_blocks WHERE code=?", (code,)
            ).require_one()
            tail = _SequenceTail((int(row[0]) + 1) * _BLOCK_STEPS if row[0] is not None else 0)
        self._tails[code] = tail
        stride = self._layout.channels * 8
        overlap = _overlap(tail.previous, payload, stride)
        start = tail.next_position - overlap
        tail.pending.extend(payload[overlap * stride :])
        tail.next_position += self._layout.steps - overlap
        tail.previous = payload
        block_bytes = _BLOCK_STEPS * stride
        while len(tail.pending) >= block_bytes:
            block = (tail.next_position - len(tail.pending) // stride) // _BLOCK_STEPS
            self._write(code, block, bytes(tail.pending[:block_bytes]))
            del tail.pending[:block_bytes]
        return start

    def finish(self) -> None:
        for code, tail in self._tails.items():
            self._flush(code, tail)
        self._tails.clear()

    def _flush(self, code: str, tail: _SequenceTail) -> None:
        if tail.pending:
            self._write(code, tail.next_position // _BLOCK_STEPS, bytes(tail.pending))
            tail.pending.clear()

    def _write(self, code: str, block: int, payload: bytes) -> None:
        self._connection.run("INSERT INTO sample_sequence_blocks VALUES (?, ?, ?)", (code, block, payload))


def _overlap(previous: bytes, current: bytes, stride: int) -> int:
    if not previous:
        return 0
    # Consecutive market dates dominate. Missing candidate dates still share exact suffixes.
    steps = len(current) // stride
    if previous[stride:] == current[:-stride]:
        return steps - 1
    if previous == current:
        return steps
    for count in range(steps - 2, 0, -1):
        if previous[-count * stride :] == current[: count * stride]:
            return count
    return 0


class SampleSequenceReader:
    """Retain 32 MiB plus one in-flight batch; avoid per-sample random SQL reads."""

    def __init__(self, connection: SqliteConnection, layout: SampleSequenceLayout) -> None:
        self._connection = connection
        self._layout = layout
        self._blocks: OrderedDict[tuple[str, int], np.ndarray] = OrderedDict()
        self._bytes = 0

    def read(self, windows: Sequence[tuple[str, int]]) -> np.ndarray:
        if len(windows) > 256 or any(start < 0 for _, start in windows):
            raise ValueError("sample sequence batch exceeds bounds")
        keys = tuple(
            dict.fromkeys(
                (code, block)
                for code, start in windows
                for block in range(start // _BLOCK_STEPS, (start + self._layout.steps - 1) // _BLOCK_STEPS + 1)
            )
        )
        missing = tuple(key for key in keys if key not in self._blocks)
        # 400 pairs stay below the SQLite 999-parameter portability floor.
        for offset in range(0, len(missing), 400):
            selected = missing[offset : offset + 400]
            placeholders = ",".join("(?,?)" for _ in selected)
            rows = self._connection.run(
                f"WITH wanted(code,block) AS (VALUES {placeholders}) "
                "SELECT s.code,s.block,s.payload FROM wanted w JOIN sample_sequence_blocks s "
                "ON s.code=w.code AND s.block=w.block",
                tuple(value for key in selected for value in key),
            ).fetchall()
            for code, block, payload in rows:
                if not isinstance(payload, bytes) or not 0 < len(payload) <= _BLOCK_STEPS * self._layout.channels * 8:
                    raise ValueError("sample sequence block is invalid")
                vector = np.frombuffer(payload, dtype="<f8")
                self._blocks[(str(code), int(block))] = vector
                self._bytes += vector.nbytes
        if any(key not in self._blocks for key in keys):
            raise ValueError("sample sequence block is missing")
        values: np.ndarray = np.empty((len(windows), self._layout.steps * self._layout.channels), dtype=np.float64)
        for index, (code, start) in enumerate(windows):
            position = 0
            for block in range(start // _BLOCK_STEPS, (start + self._layout.steps - 1) // _BLOCK_STEPS + 1):
                key = (code, block)
                vector = self._blocks[key]
                self._blocks.move_to_end(key)
                first = max(start - block * _BLOCK_STEPS, 0) * self._layout.channels
                count = (
                    min(
                        _BLOCK_STEPS - first // self._layout.channels,
                        self._layout.steps - position // self._layout.channels,
                    )
                    * self._layout.channels
                )
                if len(vector) < first + count:
                    raise ValueError("sample sequence block is truncated")
                values[index, position : position + count] = vector[first : first + count]
                position += count
        while self._bytes > _READER_BYTES:
            _, vector = self._blocks.popitem(last=False)
            self._bytes -= vector.nbytes
        if not np.isfinite(values).all():
            raise ValueError("sample sequence contains nonfinite values")
        return values
