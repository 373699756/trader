"""Bounded V1 matrix decoding preserves the persisted doubles and row metadata."""

import struct
from contextlib import ExitStack, closing
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pytest

from trader.infra.sqlite_connection import SqliteTransaction
from trader.recommendation.domain.market.v1.features import LEARNED_RANK_FEATURE_IDS
from trader.recommendation.domain.publication.models import Strategy
from trader.training.infra.profile.v1.samples import LearnedRankSampleIndex

_FIRST_DAY = date(2026, 1, 5)
_FEATURE_FORMAT = f"<{len(LEARNED_RANK_FEATURE_IDS)}d"


def _features(number: int) -> tuple[float, ...]:
    return (-0.0, *(float(number * 10 + position) / 8 for position in range(1, len(LEARNED_RANK_FEATURE_IDS))))


def _records() -> list[tuple[str, str, str, bytes, float | None, float | None]]:
    rows = [
        (
            _FIRST_DAY.isoformat(),
            f"{600000 + number:06d}",
            f"industry-{number % 3}",
            struct.pack(_FEATURE_FORMAT, *_features(number)),
            number / 16,
            -number / 32,
        )
        for number in range(4_097)
    ]
    rows.extend(
        (
            (_FIRST_DAY + timedelta(days=1)).isoformat(),
            f"{600000 + number:06d}",
            f"industry-{number % 3}",
            struct.pack(_FEATURE_FORMAT, *_features(5_000 + number)),
            None if number in (0, 2) else number / 16,
            None if number in (1, 2) else -number / 32,
        )
        for number in range(5)
    )
    return rows


@pytest.mark.parametrize("strategy,target_position", ((Strategy.TOMORROW, 4), (Strategy.D25, 5)))
def test_matrix_preserves_bits_order_groups_and_independent_missing_targets_across_batches(
    tmp_path: Path, strategy: Strategy, target_position: int
) -> None:
    records = _records()
    expected = sorted((row for row in records if row[target_position] is not None), key=lambda row: (row[0], row[1]))
    with closing(LearnedRankSampleIndex(tmp_path / "samples.sqlite3")) as index:
        with SqliteTransaction(index._connection):
            index._connection.run_many("INSERT INTO samples VALUES (?, ?, ?, ?, ?, ?)", reversed(records))
        with ExitStack() as resources:
            matrix = index.matrix(
                (_FIRST_DAY, _FIRST_DAY + timedelta(days=1)), strategy, tmp_path / "matrix", resources
            )
            assert matrix.features.astype("<f8", copy=False).tobytes() == b"".join(row[3] for row in expected)
            np.testing.assert_array_equal(matrix.targets, [row[target_position] for row in expected])
            assert matrix.codes == tuple(row[1] for row in expected)
            assert matrix.dates == tuple(date.fromisoformat(row[0]) for row in expected)
            assert matrix.industries == tuple(row[2] for row in expected)
            assert matrix.groups == (4_097, 3)
            assert matrix.features.flags.writeable and matrix.targets.flags.writeable


@pytest.mark.parametrize("number", (0, 4_096))
@pytest.mark.parametrize("first_is_short", (False, True))
def test_matrix_rejects_compensating_short_and_long_blobs_in_each_batch(
    tmp_path: Path, number: int, first_is_short: bool
) -> None:
    records = _records()
    first, second = records[number : number + 2]
    first_blob = first[3][:-8] if first_is_short else first[3] + b"\0" * 8
    second_blob = second[3] + b"\0" * 8 if first_is_short else second[3][:-8]
    records[number] = (*first[:3], first_blob, *first[4:])
    records[number + 1] = (*second[:3], second_blob, *second[4:])
    with closing(LearnedRankSampleIndex(tmp_path / "samples.sqlite3")) as index:
        with SqliteTransaction(index._connection):
            index._connection.run_many("INSERT INTO samples VALUES (?, ?, ?, ?, ?, ?)", records)
        with ExitStack() as resources, pytest.raises(struct.error, match="unpack requires a buffer of 80 bytes"):
            index.matrix((_FIRST_DAY, _FIRST_DAY + timedelta(days=1)), Strategy.D25, tmp_path / "matrix", resources)


def test_matrix_rejects_rows_between_nonconsecutive_partition_dates(tmp_path: Path) -> None:
    with closing(LearnedRankSampleIndex(tmp_path / "samples.sqlite3")) as index:
        with SqliteTransaction(index._connection):
            index._connection.run_many("INSERT INTO samples VALUES (?, ?, ?, ?, ?, ?)", _records())
        with ExitStack() as resources, pytest.raises(ValueError, match="sample crossed a date partition"):
            index.matrix(
                (_FIRST_DAY, _FIRST_DAY + timedelta(days=2)), Strategy.TOMORROW, tmp_path / "matrix", resources
            )


@pytest.mark.parametrize("strategy", (Strategy.TOMORROW, Strategy.D25))
def test_matrix_rejects_a_partition_without_a_target(tmp_path: Path, strategy: Strategy) -> None:
    with closing(LearnedRankSampleIndex(tmp_path / "samples.sqlite3")) as index:
        with SqliteTransaction(index._connection):
            index._connection.run(
                "INSERT INTO samples VALUES (?, ?, ?, ?, ?, ?)",
                (_FIRST_DAY.isoformat(), "600001", "industry", struct.pack(_FEATURE_FORMAT, *_features(1)), None, None),
            )
        with ExitStack() as resources, pytest.raises(ValueError, match="learned_rank_sample_partition_empty"):
            index.matrix((_FIRST_DAY,), strategy, tmp_path / "matrix", resources)
