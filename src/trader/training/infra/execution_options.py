"""Explicit execution candidates; ordinary training retains the existing baseline policy."""

from dataclasses import dataclass


@dataclass(frozen=True)
class TrainingExecutionOptions:
    memory_first: bool = False
    release_cpu_before_publish: bool = False
    shared_matrix_memory: bool = False
    shared_validation: bool = False
    normalized_tcn_batches: bool = False

    def __post_init__(self) -> None:
        if any(type(value) is not bool for value in (
            self.memory_first,
            self.release_cpu_before_publish,
            self.shared_matrix_memory,
            self.shared_validation,
            self.normalized_tcn_batches,
        )):
            raise ValueError("training_execution_options_invalid")


BASELINE_EXECUTION_OPTIONS = TrainingExecutionOptions()


__all__ = [
    "BASELINE_EXECUTION_OPTIONS",
    "TrainingExecutionOptions",
]
