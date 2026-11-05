"""Product-free names and canonical parsing for explicit diagnostic experiments."""

from typing import Literal

ExecutionCandidate = Literal[
    "baseline", "resources", "ram_matrices", "shared_validation", "normalized_tcn", "combined", "selected",
]
CpuCandidate = Literal["baseline", "two_plus_one", "three_single"]
ExecutionComponent = Literal["resources", "ram_matrices", "shared_validation", "normalized_tcn"]
FanoutCandidate = Literal["baseline", "batch_double", "queue_double"]
_COMPONENT_ORDER: tuple[ExecutionComponent, ...] = (
    "resources", "ram_matrices", "shared_validation", "normalized_tcn",
)


def ParseExecutionComponents(value: str) -> tuple[ExecutionComponent, ...]:
    """Parse a nonempty comma-separated subset into the fixed experiment order."""
    if not isinstance(value, str):
        raise ValueError("training_execution_components_invalid")
    parts = tuple(part.strip() for part in value.split(","))
    if any(part not in _COMPONENT_ORDER for part in parts) or len(set(parts)) != len(parts):
        raise ValueError("training_execution_components_invalid")
    return tuple(component for component in _COMPONENT_ORDER if component in parts)


__all__ = [
    "CpuCandidate",
    "ExecutionCandidate",
    "ExecutionComponent",
    "FanoutCandidate",
    "ParseExecutionComponents",
]
