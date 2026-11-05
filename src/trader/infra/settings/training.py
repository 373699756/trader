"""One training configuration schema, parsed only for offline training."""

from collections.abc import Mapping
from pathlib import Path

from trader.infra.disk_types import DiskKind
from trader.infra.settings.parser import (
    ConfigurationError,
    Integer,
    ReadJsonObject,
    RequireExactKeys,
    RequireMapping,
)
from trader.training.application.resources import (
    TrainingCpuSettings,
    TrainingFanoutSettings,
    TrainingMemorySettings,
    TrainingResourceSettings,
    TrainingSampleSettings,
)


def ReadTrainingConfiguration(path: Path) -> dict[str, object]:
    document = ReadJsonObject(path)
    RequireExactKeys(document, {"profiles", "resources"}, "training")
    return document


def LoadTrainingResourceSettings(project_root: Path, medium: DiskKind) -> TrainingResourceSettings:
    document = ReadTrainingConfiguration(project_root / "config" / "training.json")
    profiles = RequireMapping(document, "resources")
    RequireExactKeys(profiles, {"hdd", "ssd"}, "training.resources")
    hdd = _resource_settings(RequireMapping(profiles, "hdd"))
    ssd = _resource_settings(RequireMapping(profiles, "ssd"))
    if medium not in {"hdd", "ssd", "unknown"}:
        raise ConfigurationError("training resource medium must be hdd, ssd or unknown")
    # Match the existing explicit conservative disk policy for unknown devices.
    return ssd if medium == "ssd" else hdd


def _resource_settings(values: Mapping[str, object]) -> TrainingResourceSettings:
    RequireExactKeys(values, {"cpu", "memory", "fanout", "samples"}, "training.resources")
    cpu = RequireMapping(values, "cpu")
    RequireExactKeys(cpu, {"coordinator_cpus", "maximum_threads", "native_threads"}, "training.resources.cpu")
    memory = RequireMapping(values, "memory")
    RequireExactKeys(
        memory, {"maximum_bytes", "reserve_bytes", "minimum_available_bytes"}, "training.resources.memory"
    )
    fanout = RequireMapping(values, "fanout")
    RequireExactKeys(fanout, {"batch_rows", "queue_batches"}, "training.resources.fanout")
    samples = RequireMapping(values, "samples")
    RequireExactKeys(samples, {
        "sqlite_cache_bytes", "sqlite_mmap_bytes", "matrix_memory_bytes", "sequence_cache_bytes",
        "sequence_writer_codes", "v1_sqlite_cache_bytes", "v1_batch_rows", "tcn_batch_memory_bytes",
    }, "training.resources.samples")
    maximum_threads = None if cpu["maximum_threads"] is None else Integer(cpu, "maximum_threads", minimum=1)
    maximum_bytes = Integer(memory, "maximum_bytes", minimum=1)
    minimum_available_bytes = Integer(memory, "minimum_available_bytes", minimum=1)
    if minimum_available_bytes > maximum_bytes:
        raise ConfigurationError("training.resources.memory.minimum_available_bytes exceeds maximum_bytes")
    memory_settings = TrainingMemorySettings(
        maximum_bytes, Integer(memory, "reserve_bytes", minimum=0), minimum_available_bytes
    )
    return TrainingResourceSettings(
        TrainingCpuSettings(
            Integer(cpu, "coordinator_cpus", minimum=0), maximum_threads, Integer(cpu, "native_threads", minimum=1)
        ),
        memory_settings,
        TrainingFanoutSettings(Integer(fanout, "batch_rows", minimum=1), Integer(fanout, "queue_batches", minimum=1)),
        TrainingSampleSettings(
            Integer(samples, "sqlite_cache_bytes", minimum=1024),
            Integer(samples, "sqlite_mmap_bytes", minimum=0),
            Integer(samples, "matrix_memory_bytes", minimum=0),
            Integer(samples, "sequence_cache_bytes", minimum=0),
            Integer(samples, "sequence_writer_codes", minimum=1),
            Integer(samples, "v1_sqlite_cache_bytes", minimum=1024),
            Integer(samples, "v1_batch_rows", minimum=1),
            Integer(samples, "tcn_batch_memory_bytes", minimum=0),
        ),
    )
