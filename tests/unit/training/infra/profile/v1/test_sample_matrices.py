"""Named V1 columns preserve exact doubles, target masks and matrix lifetimes."""

import math
import sqlite3
from contextlib import ExitStack, closing
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pytest

from trader.infra.sqlite_codec import DecodeDate, EncodeDate
from trader.infra.sqlite_connection import SqliteTransaction
from trader.recommendation.domain.publication.models import Strategy
from trader.training.infra.profile.v1.samples import LEARNED_RANK_SAMPLE_COLUMNS, LearnedRankMatrixRequest, LearnedRankSampleIndex

_FIRST_DAY = date(2026, 1, 5)
_INSERT = "INSERT INTO samples VALUES (" + ",".join("?" for _ in range(16)) + ")"


def _features(number: int) -> tuple[float, ...]:
    return (-0.0, *(float(number * 10 + position) / 8 for position in range(1, 10)))


def _record(day, number, features, t1, d25):
    flags = sum(1 << i for i, value in enumerate(features) if value == 0.0 and math.copysign(1, value) < 0)
    return (EncodeDate(day), f"{600000 + number:06d}", f"industry-{number % 3}", *features, flags, t1, d25)


def _records():
    rows = [_record(_FIRST_DAY, n, _features(n), n / 16, -n / 32) for n in range(4_097)]
    rows.extend(
        _record(_FIRST_DAY + timedelta(days=1), n, _features(5_000 + n),
                None if n in (0, 2) else n / 16, None if n in (1, 2) else -n / 32)
        for n in range(5)
    )
    return rows


@pytest.mark.parametrize("strategy,target_position", ((Strategy.TOMORROW, -2), (Strategy.D25, -1)))
def test_matrix_preserves_bits_order_groups_and_independent_missing_targets_across_batches(
    tmp_path: Path, strategy: Strategy, target_position: int
) -> None:
    records = _records()
    expected = sorted((r for r in records if r[target_position] is not None), key=lambda r: (r[0], r[1]))
    with closing(LearnedRankSampleIndex(tmp_path / "samples.sqlite3")) as index:
        with SqliteTransaction(index._connection):
            index._connection.run_many(_INSERT, reversed(records))
        with ExitStack() as resources:
            matrix = index.read_matrices(
                (LearnedRankMatrixRequest((_FIRST_DAY, _FIRST_DAY + timedelta(days=1)), strategy),),
                tmp_path / "matrix", resources,
            )[0]
            expected_features = np.asarray([r[3:13] for r in expected], dtype="<f8")
            assert matrix.features.astype("<f8", copy=False).tobytes() == expected_features.tobytes()
            np.testing.assert_array_equal(matrix.targets, [r[target_position] for r in expected])
            assert matrix.codes == tuple(r[1] for r in expected)
            assert matrix.dates == tuple(DecodeDate(r[0]) for r in expected)
            assert matrix.industries == tuple(r[2] for r in expected)
            assert matrix.groups == (4_097, 3)
            assert matrix.features.flags.writeable and matrix.targets.flags.writeable
            buffer = matrix.features.base
        assert buffer.closed


def test_named_features_reject_missing_values_and_invalid_negative_zero_flags(tmp_path):
    with closing(LearnedRankSampleIndex(tmp_path / "samples.sqlite3")) as index:
        with SqliteTransaction(index._connection):
            index._connection.run_many(_INSERT, _records())
            with pytest.raises(sqlite3.IntegrityError):
                index._connection.run(f"UPDATE samples SET {LEARNED_RANK_SAMPLE_COLUMNS[0]}=NULL")
            index._connection.run("UPDATE samples SET zero_signs=2")
        with ExitStack() as resources, pytest.raises(ValueError, match="zero_signs_invalid"):
            index.read_matrices((LearnedRankMatrixRequest((_FIRST_DAY,), Strategy.TOMORROW),), tmp_path / "matrix", resources)


def test_dual_head_matrices_share_one_scan_but_have_independent_writable_features(tmp_path):
    with closing(LearnedRankSampleIndex(tmp_path / "samples.sqlite3")) as index:
        with SqliteTransaction(index._connection):
            index._connection.run_many(_INSERT, _records())
        statements = []
        index._connection.observe_statements(statements.append)
        with ExitStack() as resources:
            first, second = index.read_matrices(
                tuple(LearnedRankMatrixRequest((_FIRST_DAY,), strategy) for strategy in (Strategy.TOMORROW, Strategy.D25)),
                tmp_path / "matrices", resources,
            )
            assert len([sql for sql in statements if sql.startswith("SELECT trade_date,code,industry")]) == 1
            expected = second.features.tobytes()
            first.features[:] = 123
            assert second.features.tobytes() == expected
            buffers = (first.features.base, second.features.base)
        assert all(buffer.closed for buffer in buffers)


def test_matrix_rejects_rows_between_nonconsecutive_partition_dates(tmp_path: Path) -> None:
    with closing(LearnedRankSampleIndex(tmp_path / "samples.sqlite3")) as index:
        with SqliteTransaction(index._connection):
            index._connection.run_many(_INSERT, _records())
        with ExitStack() as resources, pytest.raises(ValueError, match="sample crossed a date partition"):
            index.read_matrices(
                (LearnedRankMatrixRequest((_FIRST_DAY, _FIRST_DAY + timedelta(days=2)), Strategy.TOMORROW),),
                tmp_path / "matrix", resources,
            )


@pytest.mark.parametrize("strategy", (Strategy.TOMORROW, Strategy.D25))
def test_matrix_rejects_a_partition_without_a_target(tmp_path: Path, strategy: Strategy) -> None:
    with closing(LearnedRankSampleIndex(tmp_path / "samples.sqlite3")) as index:
        with SqliteTransaction(index._connection):
            index._connection.run(_INSERT, _record(_FIRST_DAY, 1, _features(1), None, None))
        with ExitStack() as resources, pytest.raises(ValueError, match="learned_rank_sample_partition_empty"):
            index.read_matrices((LearnedRankMatrixRequest((_FIRST_DAY,), strategy),), tmp_path / "matrix", resources)
