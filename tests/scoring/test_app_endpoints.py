from unittest.mock import patch

from helpers import app_patch_context
from stock_analyzer import config
from stock_analyzer.strategy_validation import StrategyValidationStore


def test_portfolio_endpoint_contract_is_explicit_404(tmp_path):
    with app_patch_context(tmp_path) as app:
        response = app.test_client().get("/api/portfolio?strategy=tomorrow_picks")

    assert response.status_code == 404


def test_portfolio_endpoint_ignores_latest_saved_snapshot_and_returns_404(tmp_path):
    rows = [
        {
            "rank": index + 1,
            "code": f"60000{index}",
            "name": f"样本{index}",
            "price": 10 + index,
            "score": 90 - index,
            "theme": ["半导体", "算力", "军工", "医药"][index],
            "serenity_profile": {"confidence_score": 80 - index, "risk_score": 40 + index},
        }
        for index in range(4)
    ]
    validation_path = tmp_path / "validation.sqlite3"
    StrategyValidationStore(str(validation_path)).save_signals(
        "tomorrow_picks",
        "tomorrow_picks_v2",
        "2024-01-01T14:30:00",
        rows,
    )

    with patch.object(config, "PORTFOLIO_SINGLE_CAP", 0.4), patch.object(
        config,
        "PORTFOLIO_THEME_CAP",
        0.7,
    ), app_patch_context(tmp_path, VALIDATION_DB_PATH=str(validation_path)) as app:
        response = app.test_client().get("/api/portfolio?strategy=tomorrow_picks")

    assert response.status_code == 404


def test_strategy_validation_readiness_endpoint_reports_zero_oos_blockers(tmp_path):
    validation_path = tmp_path / "validation.sqlite3"
    with app_patch_context(tmp_path, VALIDATION_DB_PATH=str(validation_path)) as app:
        response = app.test_client().get("/api/strategy-validation/readiness")

    payload = response.get_json()
    assert response.status_code == 200
    assert payload["ok"] is True
    assert payload["ready"] is False
    assert payload["readiness"]["real_oos_day_count"] == 0
    assert payload["table_counts"]["strategy_outcomes"] == 0
    blocker_tasks = {item["task"] for item in payload["blockers"]}
    assert "P3-REAL-OOS-SAMPLE-GATE" in blocker_tasks
    assert "P4-REBUILDABLE-RETURN-ARTIFACT" in blocker_tasks
    assert "P5-PORTFOLIO-ABLATION-EVIDENCE" in blocker_tasks
    assert "P6-DEEPSEEK-EVENT-COUNTERFACTUAL" in blocker_tasks
    assert "P7-GRAY-ROLLBACK" in blocker_tasks
