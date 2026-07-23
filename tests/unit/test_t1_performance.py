from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import pytest

from trader.entrypoints import performance as performance_module
from trader.entrypoints.performance import run_performance_check
from trader.infra.settings import load_runtime_settings

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_CONFIG = PROJECT_ROOT / "config" / "v2" / "runtime.json"
FIXTURE = PROJECT_ROOT / "tests" / "fixtures" / "performance" / "v17"


def test_market_normalization_metric_calls_production_normalizer(monkeypatch: pytest.MonkeyPatch) -> None:
    normalizer = Mock(wraps=performance_module.normalize_quotes)
    monkeypatch.setattr(performance_module, "normalize_quotes", normalizer)
    operations = performance_module._market_data_operations(  # noqa: SLF001
        performance_module._market_rows(3, 1),  # noqa: SLF001
        2,
    )
    setup_call_count = normalizer.call_count

    operations["market_normalization"]()

    assert normalizer.call_count == setup_call_count + 1


def test_perf_check_uses_production_api_and_sse_paths() -> None:
    runtime = load_runtime_settings(RUNTIME_CONFIG)

    report = run_performance_check(
        FIXTURE,
        suite="api-sse",
        budgets=runtime.performance_budgets,
        config_path=RUNTIME_CONFIG,
    )

    assert report["status"] == "passed"
    assert report["network_calls"] == 0
    assert report["workloads"] == {
        "market_rows": 5500,
        "candidate_rows": 360,
        "candidate_quote_rows": 120,
        "topk_overlay_rows": 18,
        "strategies": 3,
    }
    assert report["operation_provenance"] == {
        "sse_delivery": "trader.application.publisher.SnapshotPublisher.publish_overlay",
        "snapshot_api": "trader.web.routes_recommendations.create_recommendation_blueprint",
        "etag_api": "trader.web.routes_recommendations.create_recommendation_blueprint",
        "dates_api": "trader.web.routes_recommendations.create_recommendation_blueprint",
        "status_api": "trader.web.routes_status.create_status_blueprint",
    }
