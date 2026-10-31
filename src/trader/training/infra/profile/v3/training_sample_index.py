"""V3-sized adapter over the shared dynamic training sample partition."""

from pathlib import Path

from trader.infra.sqlite_policy import SqliteIoPolicy

from trader.training.infra.profile.v3.contracts import TOMORROW_HEAD_CONTRACT

from trader.training.infra.sample_index import (
    TRAINING_SAMPLE_CACHE_MIB as V3_SAMPLE_CACHE_MIB,
)
from trader.training.infra.sample_index import (
    TRAINING_SAMPLE_MMAP_BYTES as V3_SAMPLE_MMAP_BYTES,
)
from trader.training.infra.sample_index import (
    SQLiteTrainingSampleIndex,
)
from trader.training.infra.sample_index import (
    TargetMetric as V3TargetMetric,
)
from trader.training.infra.sample_index import (
    TrainingIndustryCounts as V3TrainingIndustryCounts,
)
from trader.training.infra.sample_index import (
    TrainingIndustryData as V3TrainingIndustryData,
)
from trader.training.infra.sample_index import (
    TrainingSample as V3TrainingSample,
)
from trader.training.infra.sample_index import (
    TrainingSampleMatrix as V3TrainingSampleMatrix,
)
from trader.training.infra.sample_index import (
    TrainingSplitName as V3TrainingSplitName,
)


class SQLiteV3TrainingSampleIndex(SQLiteTrainingSampleIndex):
    def __init__(self, path: Path, *, sqlite_policy: SqliteIoPolicy) -> None:
        super().__init__(path, TOMORROW_HEAD_CONTRACT.feature_manifest.names, sqlite_policy=sqlite_policy)


__all__ = [
    "SQLiteV3TrainingSampleIndex",
    "V3_SAMPLE_CACHE_MIB",
    "V3_SAMPLE_MMAP_BYTES",
    "V3TargetMetric",
    "V3TrainingIndustryCounts",
    "V3TrainingIndustryData",
    "V3TrainingSample",
    "V3TrainingSampleMatrix",
    "V3TrainingSplitName",
]
