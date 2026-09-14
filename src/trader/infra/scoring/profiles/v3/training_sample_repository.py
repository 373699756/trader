"""V3-sized adapter over the shared dynamic training sample repository."""

from pathlib import Path

from trader.infra.scoring.training.sample_repository import (
    TRAINING_SAMPLE_CACHE_MIB as V3_SAMPLE_CACHE_MIB,
)
from trader.infra.scoring.training.sample_repository import (
    TRAINING_SAMPLE_MMAP_BYTES as V3_SAMPLE_MMAP_BYTES,
)
from trader.infra.scoring.training.sample_repository import (
    SQLiteTrainingSampleRepository,
)
from trader.infra.scoring.training.sample_repository import (
    TargetMetric as V3TargetMetric,
)
from trader.infra.scoring.training.sample_repository import (
    TrainingIndustryCounts as V3TrainingIndustryCounts,
)
from trader.infra.scoring.training.sample_repository import (
    TrainingIndustryData as V3TrainingIndustryData,
)
from trader.infra.scoring.training.sample_repository import (
    TrainingSample as V3TrainingSample,
)
from trader.infra.scoring.training.sample_repository import (
    TrainingSampleMatrix as V3TrainingSampleMatrix,
)
from trader.infra.scoring.training.sample_repository import (
    TrainingSplitName as V3TrainingSplitName,
)


class SQLiteV3TrainingSampleRepository(SQLiteTrainingSampleRepository):
    def __init__(self, path: Path) -> None:
        super().__init__(path, 6)


__all__ = [
    "SQLiteV3TrainingSampleRepository",
    "V3_SAMPLE_CACHE_MIB",
    "V3_SAMPLE_MMAP_BYTES",
    "V3TargetMetric",
    "V3TrainingIndustryCounts",
    "V3TrainingIndustryData",
    "V3TrainingSample",
    "V3TrainingSampleMatrix",
    "V3TrainingSplitName",
]
