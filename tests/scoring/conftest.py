from __future__ import annotations

import pytest

from stock_analyzer import config

from helpers import (
    app_patch_context,
    code_set,
    fake_provider,
    history_frame,
    make_validation_store,
    quote_frame,
    score_tech_potential_candidates,
    validation_history,
    write_json,
    write_text,
)


INTEGRATION_FILES = {
    "test_app_endpoints.py",
    "test_app_recommendations.py",
    "test_legacy_remaining.py",
    "test_portfolio_risk.py",
    "test_provider_fallbacks.py",
    "test_snapshot_jobs.py",
    "test_stock_prediction_api.py",
    "test_validation_backfill.py",
    "test_validation_oos.py",
    "test_validation_repository_runtime.py",
}

SLOW_FILES = {
    "test_app_recommendations.py",
    "test_legacy_remaining.py",
    "test_snapshot_jobs.py",
    "test_validation_repository_runtime.py",
}

SLOW_NAME_PARTS = (
    "backfill",
    "live_weight_calibration",
    "paper_trading",
    "prefetch_history",
    "recommendations_",
    "run_snapshot",
    "validation_metrics",
    "validation_primary_metrics",
)


@pytest.fixture
def quotes():
    return quote_frame


@pytest.fixture
def codes():
    return code_set


@pytest.fixture
def risk_blacklist_files(tmp_path, monkeypatch):
    json_path = tmp_path / "risk_blacklist.json"
    csv_path = tmp_path / "risk_blacklist.csv"
    monkeypatch.setattr(config, "RISK_BLACKLIST_PATH", str(json_path))
    monkeypatch.setattr(config, "RISK_BLACKLIST_CSV_PATH", "")
    monkeypatch.setattr(config, "RISK_BLACKLIST_HARD_FILTER", True)
    return {
        "json": json_path,
        "csv": csv_path,
        "write_json": write_json,
        "write_text": write_text,
    }


@pytest.fixture
def make_history_frame():
    return history_frame


@pytest.fixture
def make_validation_history():
    return validation_history


@pytest.fixture
def make_fake_provider():
    return fake_provider


@pytest.fixture
def validation_store(tmp_path):
    return make_validation_store(tmp_path)


@pytest.fixture
def patched_app(tmp_path):
    def _factory(**overrides):
        return app_patch_context(tmp_path, **overrides)

    return _factory


def _install_legacy_helpers(module):
    if module is None:
        return
    if not hasattr(module, "_validation_history"):
        module._validation_history = validation_history
    if not hasattr(module, "score_tech_potential_candidates"):
        module.score_tech_potential_candidates = score_tech_potential_candidates


def pytest_collection_modifyitems(items):
    for item in items:
        filename = item.path.name
        if filename in INTEGRATION_FILES:
            item.add_marker(pytest.mark.integration)
        if filename in SLOW_FILES or any(part in item.name for part in SLOW_NAME_PARTS):
            item.add_marker(pytest.mark.slow)
        _install_legacy_helpers(getattr(item, "module", None))


def pytest_runtest_setup(item):
    _install_legacy_helpers(getattr(item, "module", None))
