from __future__ import annotations

from argparse import Namespace

from scripts.runtime_diagnostics.history_sources import (
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
    }
    values.update(overrides)
    return Namespace(**values)


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
