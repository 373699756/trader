"""One industry cache, preserving float64 values and chronological batch boundaries."""

import mmap
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast

import numpy as np

from trader.training.infra.model_bundles.contracts import TrainedHeadContract
from trader.training.infra.sample_index import (
    SQLiteTrainingSampleIndex,
    TrainingIndustryCounts,
    TrainingSampleMatrix,
    TrainingSplitName,
)

TCN_BATCH_CACHE_BYTES = 256 * 1024**2


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
            if size > TCN_BATCH_CACHE_BYTES
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
