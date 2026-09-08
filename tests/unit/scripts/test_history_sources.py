from __future__ import annotations

from argparse import Namespace

import pytest

from scripts.runtime_diagnostics.history_sources import (
    PROJECT_ROOT,
    HistoryObservation,
    _validate,
    build_report,
)


def _args(**overrides: object) -> Namespace:
    values: dict[str, object] = {
        "codes": ["600519"],
        "samples": 1,
        "workers": 1,
        "days": 61,
        "timeout_seconds": 4.5,
        "source": "composite",
        "tencent_history_host": "proxy",
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


def test_history_report_requires_feature_and_outcome_pair_coverage() -> None:
    observation = HistoryObservation(
        sample=1,
        code="600519",
        source="composite",
        selected_source="tencent",
        row_count=61,
        outcome_selected_source="tencent",
        outcome_pair_count=0,
        latency_ms=12.5,
        error=None,
        outcome_error=None,
        bars=(),
    )

    report = build_report(("600519",), (observation,), _args())

    assert report["status"] == "degraded"
    assert report["summary"]["feature_usable_observations"] == 1
    assert report["summary"]["outcome_pair_usable_observations"] == 0
    assert report["observations"][0]["outcome_pair_count"] == 0
