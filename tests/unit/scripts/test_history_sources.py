from __future__ import annotations

from argparse import Namespace

import pytest

from scripts.runtime_diagnostics.history_sources import PROJECT_ROOT, _validate


def _args(**overrides: object) -> Namespace:
    values: dict[str, object] = {
        "codes": ["600519"],
        "samples": 1,
        "workers": 1,
        "days": 61,
        "timeout_seconds": 4.5,
        "persistence_runtime_dir": None,
    }
    values.update(overrides)
    return Namespace(**values)


def test_persistence_measurement_rejects_relative_or_repository_paths() -> None:
    with pytest.raises(ValueError, match="absolute path outside"):
        _validate(_args(persistence_runtime_dir="relative-runtime"))
    with pytest.raises(ValueError, match="absolute path outside"):
        _validate(_args(persistence_runtime_dir=str(PROJECT_ROOT / "diagnostic-runtime")))


def test_persistence_measurement_accepts_explicit_external_path() -> None:
    assert _validate(_args(persistence_runtime_dir="/tmp/trader-history-diagnostic")) == ("600519",)
