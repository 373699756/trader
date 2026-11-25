"""The single numerical document for a D25 strategy model."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from trader.infra.regular_file import ReadRegularFile

_MAXIMUM_MODEL_BYTES = 160 * 1024**2


@dataclass(frozen=True, slots=True)
class D25ModelDocument:
    utility: Mapping[str, object]
    probability: Mapping[str, object] | None = None


def EncodeD25ModelDocument(document: D25ModelDocument) -> dict[str, object]:
    return {
        "utility": dict(document.utility),
        "probability": dict(document.probability) if document.probability is not None else None,
    }


def ReadD25ModelDocument(path: Path) -> D25ModelDocument:
    value: object = json.loads(ReadRegularFile(path, maximum_bytes=_MAXIMUM_MODEL_BYTES))
    if not isinstance(value, dict) or set(value) != {"utility", "probability"}:
        raise ValueError("d25_model_document_fields_invalid")
    utility: object = value["utility"]
    probability: object = value["probability"]
    if not isinstance(utility, dict) or (probability is not None and not isinstance(probability, dict)):
        raise ValueError("d25_model_document_fields_invalid")
    return D25ModelDocument(utility, probability)
