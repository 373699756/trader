"""Checked primitive values for D25 artifact documents."""

import math
from collections.abc import Mapping


def D25ArtifactObject(value: object, keys: set[str]) -> Mapping[str, object]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError("d25_probability_artifact_fields_invalid")
    return value


def D25ArtifactNumber(value: object) -> float:
    if type(value) not in (int, float):
        raise ValueError("d25_probability_number_invalid")
    assert isinstance(value, (int, float))
    try:
        number = float(value)
    except OverflowError as exc:
        raise ValueError("d25_probability_number_invalid") from exc
    if not math.isfinite(number):
        raise ValueError("d25_probability_number_invalid")
    return number


def D25ArtifactVector(value: object, count: int) -> tuple[float, ...]:
    if not isinstance(value, list) or len(value) != count:
        raise ValueError("d25_probability_vector_invalid")
    return tuple(D25ArtifactNumber(item) for item in value)
