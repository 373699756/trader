"""One industry cache, preserving float64 values and chronological batch boundaries."""

from __future__ import annotations

import mmap
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, cast

import numpy as np

from trader.training.infra.model_bundles.contracts import TrainedHeadContract
from trader.training.infra.profile.v5.settings import TcnComputeDtype
from trader.training.infra.sample_index import (
    SQLiteTrainingSampleIndex,
    TrainingIndustryCounts,
    TrainingSampleMatrix,
    TrainingSplitName,
)

if TYPE_CHECKING:
    import torch


@dataclass(frozen=True)
class TcnBatchCache:
    training: np.ndarray
    early_stopping: np.ndarray
    batch_size: int

    def batches(self, split: TrainingSplitName) -> Iterator[TrainingSampleMatrix]:
        if split not in {"training", "early_stopping"}:
            raise ValueError("tcn_cache_partition_invalid")
        values = self.training if split == "training" else self.early_stopping
        for start in range(0, len(values), self.batch_size):
            batch = values[start : start + self.batch_size]
            yield TrainingSampleMatrix(batch[:, :-1], batch[:, -1])


@dataclass(frozen=True)
class TcnPreparedBatches:
    training: torch.Tensor
    training_labels: torch.Tensor
    early_stopping: torch.Tensor
    early_stopping_labels: np.ndarray
    batch_size: int

    def training_batches(self) -> Iterator[tuple[torch.Tensor, torch.Tensor]]:
        for start in range(0, len(self.training), self.batch_size):
            end = start + self.batch_size
            yield self.training[start:end], self.training_labels[start:end]

    def early_stopping_batches(self) -> Iterator[tuple[torch.Tensor, np.ndarray]]:
        for start in range(0, len(self.early_stopping), self.batch_size):
            end = start + self.batch_size
            yield self.early_stopping[start:end], self.early_stopping_labels[start:end]


@contextmanager
def CacheTcnBatches(
    samples: SQLiteTrainingSampleIndex,
    counts: TrainingIndustryCounts,
    contract: TrainedHeadContract,
    batch_size: int,
    heartbeat: Callable[[], None],
) -> Iterator[TcnBatchCache]:
    width = len(contract.feature_positions) + 1
    size = (counts.training + counts.early_stopping) * width * 8
    with ExitStack() as scope:
        directory = (
            Path(scope.enter_context(TemporaryDirectory(prefix="tcn-batches-", dir=samples.workspace)))
            if size > samples.sample_settings.tcn_batch_memory_bytes
            else None
        )
        matrices: list[np.ndarray] = []
        for partition, count in (("training", counts.training), ("early_stopping", counts.early_stopping)):
            if count <= 0:
                raise ValueError("tcn_cache_empty_partition")
            if directory is None:
                values = np.empty((count, width), dtype=np.float64)
            else:
                with (directory / partition).open("w+b") as handle:
                    handle.truncate(count * width * 8)
                    buffer = mmap.mmap(handle.fileno(), count * width * 8)
                    scope.callback(buffer.close)
                values = np.ndarray((count, width), dtype=np.float64, buffer=buffer)
            offset = 0
            for batch in samples.iter_batches(
                counts.industry, contract, cast(TrainingSplitName, partition), batch_size=batch_size
            ):
                heartbeat()
                end = offset + len(batch.labels)
                if end > count:
                    raise ValueError("tcn_cache_sample_count_changed")
                values[offset:end, :-1] = batch.features
                values[offset:end, -1] = batch.labels
                offset = end
            if offset != count:
                raise ValueError("tcn_cache_sample_count_changed")
            values.flags.writeable = False
            matrices.append(values)
        yield TcnBatchCache(matrices[0], matrices[1], batch_size)


def _prepared_size(rows: int, features: int, feature_dtype: np.dtype, label_dtype: np.dtype) -> tuple[int, int]:
    # Keep float64 early-stopping labels aligned even when a float32 feature width is odd.
    label_offset = (rows * features * feature_dtype.itemsize + 7) // 8 * 8
    return label_offset, label_offset + rows * label_dtype.itemsize


def _prepared_partition(
    rows: int,
    features: int,
    feature_dtype: np.dtype,
    label_dtype: np.dtype,
    path: Path | None,
    resources: ExitStack,
) -> tuple[np.ndarray, np.ndarray]:
    if path is None:
        return np.empty((rows, features), dtype=feature_dtype), np.empty(rows, dtype=label_dtype)
    offset, size = _prepared_size(rows, features, feature_dtype, label_dtype)
    with path.open("w+b") as handle:
        handle.truncate(size)
        buffer = mmap.mmap(handle.fileno(), size)
        resources.callback(buffer.close)
    return (
        np.ndarray((rows, features), dtype=feature_dtype, buffer=buffer),
        np.ndarray(rows, dtype=label_dtype, buffer=buffer, offset=offset),
    )


@contextmanager
def PrepareTcnBatches(
    cache: TcnBatchCache,
    means: np.ndarray,
    scales: np.ndarray,
    *,
    workspace: Path,
    memory_bytes: int,
    compute_dtype: TcnComputeDtype,
    heartbeat: Callable[[], None],
) -> Iterator[TcnPreparedBatches]:
    """Budget the raw/prepared transition and map candidate output when it does not fit."""
    # The raw-only cache stays usable when the optional tensor dependency is not installed.
    import torch

    width = cache.training.shape[1] - 1
    batch_size = cache.batch_size
    if (
        means.shape != (width,) or scales.shape != (width,)
        or cache.early_stopping.shape[1] != width + 1
        or compute_dtype not in {"float32", "float64"}
        or type(memory_bytes) is not int or memory_bytes < 0
    ):
        raise ValueError("tcn_prepared_cache_configuration_invalid")
    dtype = np.dtype(compute_dtype)
    early_label_dtype = np.dtype(np.float64)
    training_size = _prepared_size(len(cache.training), width, dtype, dtype)[1]
    stopping_size = _prepared_size(len(cache.early_stopping), width, dtype, early_label_dtype)[1]
    raw_bytes = cache.training.nbytes + cache.early_stopping.nbytes
    batch_rows = min(cache.batch_size, max(len(cache.training), len(cache.early_stopping)))
    temporary_bytes = batch_rows * (width * (16 + dtype.itemsize) + 8) + means.nbytes + scales.nbytes
    with ExitStack() as resources:
        directory = (
            Path(resources.enter_context(TemporaryDirectory(prefix="tcn-batches-", dir=workspace)))
            if raw_bytes + training_size + stopping_size + temporary_bytes > memory_bytes else None
        )
        training, training_labels = _prepared_partition(
            len(cache.training),
            width,
            dtype,
            dtype,
            directory / "training" if directory is not None else None,
            resources,
        )
        stopping, stopping_labels = _prepared_partition(
            len(cache.early_stopping),
            width,
            dtype,
            early_label_dtype,
            directory / "early_stopping" if directory is not None else None,
            resources,
        )
        for split, output, labels in (
            ("training", training, training_labels), ("early_stopping", stopping, stopping_labels),
        ):
            position = 0
            for batch in cache.batches(cast(TrainingSplitName, split)):
                heartbeat()
                end = position + len(batch.labels)
                # Keep the original float64 subtraction/division before the dtype conversion.
                output[position:end] = (batch.features - means) / scales
                labels[position:end] = batch.labels
                position = end
                del batch
            if position != len(output):
                raise ValueError("tcn_prepared_cache_sample_count_changed")
        # A suspended contextmanager otherwise retains its raw argument for every epoch.
        del cache
        # Build tensor views while the owned arrays are writable to avoid a readonly-buffer
        # warning. The network receives inputs only; backing arrays are then frozen.
        prepared = TcnPreparedBatches(
            torch.from_numpy(training),
            torch.from_numpy(training_labels),
            torch.from_numpy(stopping),
            stopping_labels,
            batch_size,
        )
        for values in (training, training_labels, stopping, stopping_labels):
            values.flags.writeable = False
        yield prepared


__all__ = [
    "CacheTcnBatches",
    "PrepareTcnBatches",
    "TcnBatchCache",
    "TcnPreparedBatches",
]
