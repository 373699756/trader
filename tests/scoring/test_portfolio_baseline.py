import json
import sqlite3
from unittest.mock import patch

import pandas as pd

from stock_analyzer import config
from stock_analyzer.app import create_app
from stock_analyzer.execution_policy import build_execution_policy
from stock_analyzer.portfolio_baseline import DailyPortfolioBaselineService
from stock_analyzer.strategy_validation import StrategyValidationStore


class FakeProvider:
    def __init__(self, histories):
        self.histories = histories

    def get_history(self, code, days=220):
        return self.histories[str(code)].copy()


def _history(next_close):
    return pd.DataFrame(
        {
            "trade_date": ["20240101", "20240102"],
            "open": [10.0, 10.0],
            "high": [10.1, max(10.1, next_close)],
            "low": [9.9, min(9.9, next_close)],
            "price": [10.0, next_close],
            "turnover": [500_000_000, 500_000_000],
        }
    )


def _candidate(code, rank, score, selected=False, challenge_score=None):
    scored = {
        "code": code,
        "name": "样本{}".format(code),
        "price": 10.0,
        "turnover": 500_000_000,
        "industry": "行业{}".format(rank % 3),
        "score": score,
        "rank": rank,
        "frozen_rule_rank": rank,
    }
    if challenge_score is not None:
        scored["challenge_score"] = challenge_score
    selected_payload = dict(scored) if selected else {}
    return {
        "code": code,
        "name": scored["name"],
        "market": "main",
        "industry": scored["industry"],
        "style_bucket": "mid_cap",
        "eligible": True,
        "selected": selected,
        "rank": rank,
        "score": score,
        "point_in_time_valid": True,
        "eligibility_reasons": [{"key": "selected" if selected else "not_selected"}],
        "feature_values": {"model_input": dict(scored)},
        "missing_mask": {},
        "source_timestamps": {"market_data_cutoff": "2024-01-01T15:00:00"},
        "market_data_cutoff": "2024-01-01T15:00:00",
        "raw": {
            "quote": {"code": code, "price": 10.0, "turnover": 500_000_000},
            "candidate": dict(scored),
            "scored": dict(scored),
            "selected": selected_payload,
        },
    }


def _build_store(tmp_path, missing_challenge_code=""):
    store = StrategyValidationStore(str(tmp_path / "validation.sqlite3"))
    candidates = [
        _candidate(
            "60000{}".format(index),
            index,
            100 - index,
            selected=index <= 3,
            challenge_score=None if "60000{}".format(index) == missing_challenge_code else index,
        )
        for index in range(1, 7)
    ]
    signals = [
        {
            **candidate["raw"]["selected"],
            "tier": "primary_watch",
            "execution_allowed": True,
        }
        for candidate in candidates
        if candidate["selected"]
    ]
    store.save_signals(
        "tomorrow_picks",
        config.TOMORROW_STRATEGY_VERSION,
        "2024-01-01T15:00:00",
        signals,
        candidate_rows=candidates,
        batch_metadata={
            "data_source_timestamp": "2024-01-01T15:00:00",
            "market_data_cutoff": "2024-01-01T15:00:00",
        },
        execution_policy=build_execution_policy("tomorrow_picks", "all"),
    )
    histories = {
        "600001": _history(10.6),
        "600002": _history(10.4),
        "600003": _history(10.2),
        "600004": _history(9.9),
        "600005": _history(9.7),
        "600006": _history(9.5),
        "000300": _history(10.1),
    }
    return store, FakeProvider(histories)


def test_daily_portfolio_baseline_is_seeded_replayable_and_idempotent(tmp_path):
    store, provider = _build_store(tmp_path)
    service = DailyPortfolioBaselineService(store)

    first = service.run(provider, "tomorrow_picks", signal_date="2024-01-01", days=20)
    first_audit = service.report("tomorrow_picks", days=20, include_audit=True)
    second = service.run(provider, "tomorrow_picks", signal_date="2024-01-01", days=20)

    report = second["report"]
    latest = service.report("tomorrow_picks", days=20, include_audit=True)["latest"]
    groups = latest["groups"]
    assert first["settled"] == 1
    assert second["settled"] == 1
    assert report["day_count"] == 1
    assert report["record_count"] == 1
    assert latest["eligible_candidate_count"] == 6
    assert latest["random_repeats"] >= 1000
    assert groups["random_equal_weight"]["path_returns_pct"] == first_audit["latest"]["groups"]["random_equal_weight"]["path_returns_pct"]
    assert len(groups["frozen_rule_top_k"]["holdings"]) == 5
    assert len(groups["current_rule_top_k"]["holdings"]) == 3
    assert groups["major_index"]["status"] == "settled"
    assert 0 <= report["rule_vs_random_percentile"] <= 100

    with sqlite3.connect(store.db_path) as conn:
        count = conn.execute("SELECT COUNT(*) FROM daily_portfolio_baselines").fetchone()[0]
    assert count == 1


def test_portfolio_baseline_persists_compact_summary_and_compressed_audit(tmp_path):
    store, provider = _build_store(tmp_path)
    service = DailyPortfolioBaselineService(store)
    service.run(provider, "tomorrow_picks", signal_date="2024-01-01", days=20)

    with sqlite3.connect(store.db_path) as conn:
        result_json, audit_blob = conn.execute(
            "SELECT result_json, audit_blob FROM daily_portfolio_baselines"
        ).fetchone()
    persisted = json.loads(result_json)
    random_group = persisted["groups"]["random_equal_weight"]
    audit = service.record("tomorrow_picks", "2024-01-01", include_audit=True)

    assert audit_blob
    assert "audit" not in persisted
    assert "path_returns_pct" not in random_group
    assert "sample_codes" not in random_group
    assert "sample_filled_codes" not in random_group
    assert len(audit["groups"]["random_equal_weight"]["path_returns_pct"]) >= 1000
    assert len(audit["groups"]["random_equal_weight"]["sample_codes"]) >= 1000
    assert len(audit["groups"]["random_equal_weight"]["sample_filled_codes"]) >= 1000


def test_portfolio_baseline_reads_legacy_full_json_without_audit_blob(tmp_path):
    store, provider = _build_store(tmp_path)
    service = DailyPortfolioBaselineService(store)
    service.run(provider, "tomorrow_picks", signal_date="2024-01-01", days=20)
    audit = service.record("tomorrow_picks", "2024-01-01", include_audit=True)

    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            "UPDATE daily_portfolio_baselines SET result_json = ?, audit_blob = NULL",
            (json.dumps(audit, ensure_ascii=False),),
        )
    restored = service.record("tomorrow_picks", "2024-01-01", include_audit=True)

    assert restored["audit"] == audit["audit"]
    assert (
        restored["groups"]["random_equal_weight"]["path_returns_pct"]
        == audit["groups"]["random_equal_weight"]["path_returns_pct"]
    )


def test_challenge_ranking_keeps_frozen_rule_and_reports_model_percentile(tmp_path):
    store, provider = _build_store(tmp_path)
    result = DailyPortfolioBaselineService(store).run(
        provider,
        "tomorrow_picks",
        signal_date="2024-01-01",
        ranking_field="challenge_score",
        model_id="reverse_score_v1",
    )

    latest = DailyPortfolioBaselineService(store).report(
        "tomorrow_picks",
        ranking_field="challenge_score",
        model_id="reverse_score_v1",
        include_audit=True,
    )["latest"]
    frozen_codes = [item["code"] for item in latest["groups"]["frozen_rule_top_k"]["holdings"]]
    model_codes = [item["code"] for item in latest["groups"]["model_top_k"]["holdings"]]
    assert frozen_codes == ["600001", "600002", "600003", "600004", "600005"]
    assert model_codes == ["600006", "600005", "600004", "600003", "600002"]
    assert latest["ranking_coverage_pct"] == 100.0
    assert latest["groups"]["random_equal_weight"]["model_percentile"] is not None


def test_incomplete_challenge_ranking_does_not_report_model_percentile(tmp_path):
    store, provider = _build_store(tmp_path, missing_challenge_code="600006")
    service = DailyPortfolioBaselineService(store)
    service.run(
        provider,
        "tomorrow_picks",
        signal_date="2024-01-01",
        ranking_field="challenge_score",
        model_id="incomplete_score_v1",
    )

    latest = service.report(
        "tomorrow_picks",
        ranking_field="challenge_score",
        model_id="incomplete_score_v1",
        include_audit=True,
    )["latest"]
    assert latest["ranking_coverage_pct"] < 100.0
    assert latest["groups"]["model_top_k"]["status"] == "unknown"
    assert latest["groups"]["random_equal_weight"]["model_percentile"] is None


def test_portfolio_baseline_api_defaults_to_summary_and_allows_audit(tmp_path):
    store, provider = _build_store(tmp_path)
    DailyPortfolioBaselineService(store).run(
        provider,
        "tomorrow_picks",
        signal_date="2024-01-01",
    )

    with patch.object(config, "VALIDATION_DB_PATH", store.db_path), patch.object(
        config, "VALIDATION_AUTO_UPDATE_ENABLED", False
    ), patch.object(config, "VALIDATION_AUTO_SNAPSHOT_ENABLED", False), patch.object(
        config, "STATE_PATH", str(tmp_path / "state.json")
    ):
        client = create_app().test_client()
        summary_response = client.get(
            "/api/strategy-validation/portfolio-baseline?strategy=tomorrow_picks&days=20"
        )
        audit_response = client.get(
            "/api/strategy-validation/portfolio-baseline?strategy=tomorrow_picks&days=20&audit=1"
        )

    summary = summary_response.get_json()["result"]
    audit = audit_response.get_json()["result"]
    assert summary_response.status_code == 200
    assert audit_response.status_code == 200
    assert summary["day_count"] == 1
    assert "path_returns_pct" not in summary["latest"]["groups"]["random_equal_weight"]
    assert len(audit["latest"]["groups"]["random_equal_weight"]["path_returns_pct"]) >= 1000


def test_portfolio_baseline_get_never_executes_even_with_execute_query(tmp_path):
    store, _provider = _build_store(tmp_path)

    with patch.object(config, "VALIDATION_DB_PATH", store.db_path), patch.object(
        config, "VALIDATION_AUTO_UPDATE_ENABLED", False
    ), patch.object(config, "VALIDATION_AUTO_SNAPSHOT_ENABLED", False), patch.object(
        config, "STATE_PATH", str(tmp_path / "state.json")
    ), patch.object(DailyPortfolioBaselineService, "run", side_effect=AssertionError("GET must be read-only")):
        response = create_app().test_client().get(
            "/api/strategy-validation/portfolio-baseline?strategy=tomorrow_picks&days=20&execute=true"
        )

    assert response.status_code == 200
    assert response.get_json()["result"]["record_count"] == 0
