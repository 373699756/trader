from datetime import datetime
from unittest.mock import patch

import pytest

from stock_analyzer import config
from stock_analyzer.deepseek.cache import DeepSeekCache
from stock_analyzer.deepseek.budget import (
    BudgetReservation,
    daily_hard_limit,
    latest_strategy_batch,
    phase_at,
    reserve_api_call,
    usage_summary,
)
from stock_analyzer.deepseek.feature_schema import (
    FEATURE_SCHEMA_VERSION,
    candidate_feature_input,
    validate_feature_response,
)
from stock_analyzer.deepseek.feature_service import DeepSeekFeatureAnalysisService
from stock_analyzer.deepseek.http_client import DeepSeekHttpResult
from stock_analyzer.deepseek.production_merge import (
    attach_and_merge_rows,
    merge_and_rank_rows,
    merge_row_score,
    today_phase,
)
from stock_analyzer.deepseek.runtime_features import attach_persisted_deepseek_features
from stock_analyzer.strategy_validation import StrategyValidationStore


def _save_pending_batch(store, batch_id: str, strategy: str, requested_at: datetime) -> None:
    store.save_deepseek_analysis_batch(
        {
            "batch_id": batch_id,
            "strategy_name": strategy,
            "cutoff_at": requested_at.isoformat(timespec="seconds"),
            "prompt_version": "test",
            "feature_schema_version": "test",
            "requested_at": requested_at.isoformat(timespec="seconds"),
            "created_at": requested_at.isoformat(timespec="seconds"),
        }
    )


def test_budget_enforces_daily_strategy_and_window_limits(tmp_path):
    store = StrategyValidationStore(str(tmp_path / "budget.sqlite3"))
    timestamp = datetime(2026, 7, 16, 9, 40)
    with (
        patch.object(config, "DEEPSEEK_DAILY_API_HARD_LIMIT", 3),
        patch.object(
            config,
            "DEEPSEEK_STRATEGY_CALL_LIMITS",
            {"today_term": 2, "tomorrow_picks": 1, "shared_preheat": 1, "emergency_reserved": 0},
        ),
        patch.object(config, "DEEPSEEK_WINDOW_CALL_LIMITS", {"today_main": 3}),
    ):
        for index in range(3):
            batch_id = f"batch-{index}"
            _save_pending_batch(store, batch_id, "today_term", timestamp)
            reservation = reserve_api_call(store, batch_id, "today_term", timestamp)
            assert reservation.allowed is (index < 2)
        summary = usage_summary(store, timestamp)

    assert summary["used"] == 2
    assert summary["usage_by_strategy"]["today_term"] == 2
    assert summary["usage_by_window"]["today_main"] == 2


def test_daily_hard_limit_cannot_be_configured_above_188():
    with patch.object(config, "DEEPSEEK_DAILY_API_HARD_LIMIT", 999):
        assert daily_hard_limit() == 188
    with patch.object(config, "DEEPSEEK_DAILY_API_HARD_LIMIT", "invalid"):
        assert daily_hard_limit() == 0


def test_feature_cache_merge_enforces_max_entries_under_lock(tmp_path):
    path = str(tmp_path / "feature-cache.json")
    cache = DeepSeekCache()
    cache.merge(
        path,
        {
            "old": {"cached_at": 1.0},
            "middle": {"cached_at": 2.0},
        },
        max_entries=2,
    )
    cache.merge(path, {"new": {"cached_at": 3.0}}, max_entries=2)

    assert set(cache.read(path)) == {"new", "middle"}


def test_budget_keeps_today_out_of_afternoon_strategy_pool(tmp_path):
    store = StrategyValidationStore(str(tmp_path / "afternoon.sqlite3"))
    timestamp = datetime(2026, 7, 16, 13, 30)
    _save_pending_batch(store, "today-afternoon", "today_term", timestamp)

    reservation = reserve_api_call(store, "today-afternoon", "today_term", timestamp)

    assert reservation.allowed is False
    assert reservation.reason == "strategy_not_allowed_in_phase"


def test_final_score_uses_75_25_formula_and_combined_penalty():
    row = {
        "code": "600001",
        "score": 80,
        "risk_penalty": 2,
        "deepseek_feature_status": "precomputed",
        "deepseek_features": {
            "deepseek_score": 100,
            "risk_penalty": 3,
            "strategy_fit": True,
            "horizon_fit": True,
            "veto": False,
            "abstain": False,
            "reason": "evidence-backed",
        },
    }

    merged = merge_row_score(row, "tomorrow_picks")

    assert merged["local_score"] == 80
    assert merged["risk_penalty"] == 5
    assert merged["final_score"] == 80
    assert merged["deepseek_production_applied"] is True


def test_api_failure_or_abstain_falls_back_to_local_score():
    local = merge_row_score(
        {"code": "600001", "score": 77, "ranking_source": "expected_return_predicted_net_return"},
        "tomorrow_picks",
    )
    abstain = merge_row_score(
        {
            "code": "600002",
            "score": 76,
            "deepseek_feature_status": "abstain",
            "deepseek_features": {"abstain": True, "deepseek_score": 99, "risk_penalty": 30},
        },
        "tomorrow_picks",
    )

    assert local["final_score"] == 77
    assert abstain["final_score"] == 76
    assert local["deepseek_score"] is None
    assert local["ranking_source"] == "expected_return_predicted_net_return"
    assert abstain["deepseek_production_applied"] is False


def test_shadow_only_and_malformed_features_cannot_change_production_result():
    reviewed = {
        "code": "600001",
        "score": 80,
        "execution_allowed": True,
        "deepseek_features": {
            "abstain": False,
            "deepseek_score": 100,
            "risk_penalty": 0,
            "strategy_fit": True,
            "horizon_fit": True,
            "veto": True,
            "valid": True,
        },
    }
    with patch.object(config, "DEEPSEEK_SHADOW_ONLY", True):
        shadow = merge_and_rank_rows([reviewed], "tomorrow_picks")[0]
    malformed = merge_row_score(
        {"code": "600002", "score": 79, "deepseek_features": {"deepseek_score": 100}},
        "tomorrow_picks",
    )

    assert shadow["final_score"] == 80
    assert shadow["deepseek_production_applied"] is False
    assert shadow["deepseek_veto"] is False
    assert shadow["execution_allowed"] is True
    assert malformed["final_score"] == 79
    assert malformed["deepseek_production_applied"] is False


def test_persisted_feature_miss_clears_stale_review_before_local_fallback():
    class EmptyStore:
        @staticmethod
        def latest_deepseek_candidate_features(*_args, **_kwargs):
            return {}

    stale = {
        "code": "600001",
        "score": 78,
        "deepseek_feature_status": "precomputed",
        "deepseek_features": {
            "abstain": False,
            "deepseek_score": 100,
            "risk_penalty": 0,
            "strategy_fit": True,
            "horizon_fit": True,
            "veto": False,
            "valid": True,
        },
        "deepseek_score": 100,
        "deepseek_production_applied": True,
    }

    attached = attach_persisted_deepseek_features(
        [stale],
        "tomorrow_picks",
        EmptyStore(),
        signal_time="2026-07-16T14:30:00",
    )
    merged = merge_row_score(attached[0], "tomorrow_picks")

    assert "deepseek_features" not in attached[0]
    assert "deepseek_score" not in attached[0]
    assert attached[0]["deepseek_feature_status"] == "local_only"
    assert merged["final_score"] == 78
    assert merged["deepseek_production_applied"] is False


def test_repeated_merge_restores_local_execution_state_after_feature_miss():
    row = {
        "code": "600001",
        "score": 90,
        "execution_allowed": True,
        "tier": "primary_watch",
        "tier_label": "重点观察",
        "trade_action": {"action": "buy", "position_size": 0.2},
        "deepseek_features": {
            "abstain": False,
            "deepseek_score": 100,
            "risk_penalty": 0,
            "strategy_fit": True,
            "horizon_fit": True,
            "veto": True,
            "valid": True,
        },
    }
    first = merge_and_rank_rows([row], "tomorrow_picks")[0]

    class EmptyStore:
        @staticmethod
        def latest_deepseek_candidate_features(*_args, **_kwargs):
            return {}

    stale = attach_persisted_deepseek_features(
        [first],
        "tomorrow_picks",
        EmptyStore(),
        signal_time="2026-07-16T14:30:00",
    )
    restored = merge_and_rank_rows(stale, "tomorrow_picks")[0]

    assert restored["execution_allowed"] is True
    assert restored["tier"] == "primary_watch"
    assert restored["trade_action"]["action"] == "buy"
    assert "non_executable_reason" not in restored


def test_veto_and_horizon_mismatch_cannot_enter_executable_top_rows():
    rows = [
        {
            "code": "veto",
            "score": 95,
            "execution_allowed": True,
            "deepseek_feature_status": "precomputed",
            "deepseek_features": {
                "abstain": False,
                "deepseek_score": 100,
                "risk_penalty": 0,
                "strategy_fit": True,
                "horizon_fit": True,
                "veto": True,
            },
        },
        {
            "code": "mismatch",
            "score": 94,
            "execution_allowed": True,
            "deepseek_feature_status": "precomputed",
            "deepseek_features": {
                "abstain": False,
                "deepseek_score": 100,
                "risk_penalty": 0,
                "strategy_fit": False,
                "horizon_fit": False,
                "veto": False,
            },
        },
        {"code": "safe", "score": 80, "execution_allowed": True},
    ]

    ranked = merge_and_rank_rows(rows, "tomorrow_picks", now=datetime(2026, 7, 16, 14, 20))
    by_code = {row["code"]: row for row in ranked}

    assert ranked[0]["code"] == "safe"
    assert by_code["veto"]["execution_allowed"] is False
    assert by_code["mismatch"]["execution_allowed"] is False


@pytest.mark.parametrize(
    ("timestamp", "code", "allowed"),
    [
        (datetime(2026, 7, 16, 9, 35), "open_observe", False),
        (datetime(2026, 7, 16, 9, 36), "main_execution", True),
        (datetime(2026, 7, 16, 10, 30), "late_execution", True),
        (datetime(2026, 7, 16, 13, 30), "afternoon_observe", False),
    ],
)
def test_today_phase_boundaries(timestamp, code, allowed):
    phase = today_phase(timestamp)

    assert phase["code"] == code
    assert phase["execution_allowed"] is allowed


def test_late_today_uncovered_high_risk_candidate_is_observation_only():
    rows = merge_and_rank_rows(
        [{"code": "600001", "score": 90, "risk_penalty": 9, "execution_allowed": True}],
        "today_term",
        now=datetime(2026, 7, 16, 10, 31),
    )

    assert rows[0]["execution_allowed"] is False
    assert "未经DeepSeek覆盖" in rows[0]["non_executable_reason"]


def test_attach_and_merge_uses_signal_time_for_today_execution_phase():
    rows = attach_and_merge_rows(
        [{"code": "600001", "score": 90, "execution_allowed": True}],
        "today_term",
        validation_store=None,
        signal_time="2026-07-16T09:35:00",
        attach_features=lambda rows, *_args, **_kwargs: list(rows),
    )

    assert rows[0]["today_phase"] == "open_observe"
    assert rows[0]["execution_allowed"] is False


def test_response_rejects_candidate_pool_outside_code():
    candidate = {
        "code": "600001",
        "data_availability": {},
        "evidence": [{"evidence_id": "m_1", "source": "point_in_time_market_data"}],
    }

    valid, errors = validate_feature_response(
        {"results": [{"code": "999999"}]},
        strategy_name="today_term",
        candidates=[candidate],
    )

    assert valid == []
    assert errors == [{"code": "999999", "reason": "unknown_or_duplicate_candidate"}]


def test_phase_classifier_matches_budget_windows():
    assert phase_at(datetime(2026, 7, 16, 9, 15)) == "shared_preheat"
    assert phase_at(datetime(2026, 7, 16, 9, 35)) == "today_open_observe"
    assert phase_at(datetime(2026, 7, 16, 9, 36)) == "today_main"
    assert phase_at(datetime(2026, 7, 16, 10, 30)) == "today_late"
    assert phase_at(datetime(2026, 7, 16, 13, 0)) == "afternoon_main"
    assert phase_at(datetime(2026, 7, 16, 14, 20)) == "final_supplement"


def test_structured_evidence_hash_ignores_cutoff_timestamp_but_tracks_market_state():
    row = {
        "code": "600001",
        "score": 80,
        "pct_chg": 2.0,
        "speed": 0.4,
        "volume_ratio": 1.5,
        "turnover_rate": 4.0,
        "amplitude": 3.0,
    }

    first = candidate_feature_input(row, "2026-07-16T09:36:00")
    second = candidate_feature_input({**row, "score": 83}, "2026-07-16T09:39:00")
    changed = candidate_feature_input({**row, "pct_chg": 3.0}, "2026-07-16T09:39:00")

    assert first["evidence_hash"] == second["evidence_hash"]
    assert first["market_state_hash"] == second["market_state_hash"]
    assert first["evidence_hash"] != changed["evidence_hash"]
    assert DeepSeekFeatureAnalysisService._feature_cache_key(
        "today_term", first, "deepseek-test"
    ) == DeepSeekFeatureAnalysisService._feature_cache_key("today_term", second, "deepseek-test")


def test_feature_service_splits_physical_calls_and_accounts_each_atomically(tmp_path):
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = cls(2026, 7, 16, 9, 40)
            return value if tz is None else value.replace(tzinfo=tz)

    candidates = [
        {
            "code": f"60000{index}",
            "name": f"batch-{index}",
            "score": 80 - index,
            "pct_chg": 2.0,
            "speed": 0.4,
            "volume_ratio": 1.5,
            "turnover_rate": 4.0,
            "amplitude": 3.0,
        }
        for index in range(5)
    ]
    store = StrategyValidationStore(str(tmp_path / "batched.sqlite3"))

    def fake_validate(_parsed, *, strategy_name, candidates):
        return (
            [
                {
                    "code": item["code"],
                    "strategy": strategy_name,
                    "schema_version": FEATURE_SCHEMA_VERSION,
                    "evidence_hash": item["evidence_hash"],
                    "evidence_ids": [item["evidence"][0]["evidence_id"]],
                    "abstain": False,
                    "valid": True,
                }
                for item in candidates
            ],
            [],
        )

    http_result = DeepSeekHttpResult(
        parsed={"results": []},
        raw={"choices": [{"message": {"content": "{}"}}]},
        usage={"prompt_tokens": 20, "completion_tokens": 30},
    )
    with (
        patch("stock_analyzer.deepseek.feature_service.datetime", FrozenDateTime),
        patch("stock_analyzer.deepseek.feature_service.feature_runtime_config") as runtime_config,
        patch("stock_analyzer.deepseek.feature_service.validate_feature_response", side_effect=fake_validate),
        patch(
            "stock_analyzer.deepseek.feature_service.FEATURE_NEWS_CONTEXT_PROVIDER.attach",
            side_effect=lambda rows: rows,
        ),
        patch(
            "stock_analyzer.deepseek.feature_service.FEATURE_HTTP_CLIENT.post_json", return_value=http_result
        ) as post_json,
        patch.object(config, "DEEPSEEK_FEATURE_API_BATCH_SIZE", 2),
        patch.object(config, "DEEPSEEK_FEATURE_REVIEW_LIMIT", 5),
        patch.object(config, "DEEPSEEK_PRE_1430_REVIEW_LIMIT", 5),
        patch.object(config, "DEEPSEEK_FEATURE_CACHE_ENABLED", False),
        patch.object(config, "DEEPSEEK_DAILY_API_HARD_LIMIT", 10),
        patch.object(config, "DEEPSEEK_STRATEGY_CALL_LIMITS", {"today_term": 10}),
        patch.object(config, "DEEPSEEK_WINDOW_CALL_LIMITS", {"today_main": 10}),
    ):
        runtime_config.return_value = {
            "enabled": True,
            "api_key": "test-key",
            "base_url": "https://example.invalid",
            "model": "deepseek-test",
            "pro_model": "deepseek-test-pro",
            "max_tokens": 800,
            "timeout_seconds": 12,
        }
        result = DeepSeekFeatureAnalysisService().analyze(
            "today_term",
            candidates,
            store,
            cutoff_at="2026-07-16T09:40:00",
            snapshot_id="snapshot-batched",
            deadline_at="2026-07-16T10:00:00",
        )

    with store.repository.connect() as conn:
        called = conn.execute(
            """
            SELECT candidate_count
            FROM deepseek_analysis_batches
            WHERE api_called = 1
            ORDER BY batch_id
            """
        ).fetchall()

    assert result["status"] == "ok"
    assert result["candidate_count"] == 5
    assert result["valid_count"] == 5
    assert result["api_call_count"] == 3
    assert [row[0] for row in called] == [2, 2, 1]
    assert usage_summary(store, FrozenDateTime.now())["used"] == 3
    assert latest_strategy_batch(store, "today_term", FrozenDateTime.now())["requested"] == 5
    assert post_json.call_count == 3
    assert max(call.kwargs["payload"]["max_tokens"] for call in post_json.call_args_list) <= 960


def test_feature_service_api_batch_size_is_hard_capped_at_eight(tmp_path):
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = cls(2026, 7, 16, 9, 40)
            return value if tz is None else value.replace(tzinfo=tz)

    candidates = [
        {
            "code": f"600{index:03d}",
            "score": 80 - index,
            "pct_chg": 2.0,
            "speed": 0.4,
            "volume_ratio": 1.5,
            "turnover_rate": 4.0,
            "amplitude": 3.0,
        }
        for index in range(9)
    ]
    store = StrategyValidationStore(str(tmp_path / "hard-batch-cap.sqlite3"))

    def fake_validate(_parsed, *, strategy_name, candidates):
        return (
            [
                {
                    "code": item["code"],
                    "strategy": strategy_name,
                    "schema_version": FEATURE_SCHEMA_VERSION,
                    "evidence_hash": item["evidence_hash"],
                    "evidence_ids": [item["evidence"][0]["evidence_id"]],
                    "abstain": False,
                    "valid": True,
                }
                for item in candidates
            ],
            [],
        )

    http_result = DeepSeekHttpResult(parsed={"results": []}, raw={}, usage={})
    with (
        patch("stock_analyzer.deepseek.feature_service.datetime", FrozenDateTime),
        patch("stock_analyzer.deepseek.feature_service.feature_runtime_config") as runtime_config,
        patch("stock_analyzer.deepseek.feature_service.validate_feature_response", side_effect=fake_validate),
        patch(
            "stock_analyzer.deepseek.feature_service.FEATURE_NEWS_CONTEXT_PROVIDER.attach",
            side_effect=lambda rows: rows,
        ),
        patch(
            "stock_analyzer.deepseek.feature_service.FEATURE_HTTP_CLIENT.post_json",
            return_value=http_result,
        ),
        patch.object(config, "DEEPSEEK_FEATURE_API_BATCH_SIZE", 32),
        patch.object(config, "DEEPSEEK_FEATURE_REVIEW_LIMIT", 9),
        patch.object(config, "DEEPSEEK_PRE_1430_REVIEW_LIMIT", 9),
        patch.object(config, "DEEPSEEK_FEATURE_CACHE_ENABLED", False),
        patch.object(config, "DEEPSEEK_DAILY_API_HARD_LIMIT", 10),
        patch.object(config, "DEEPSEEK_STRATEGY_CALL_LIMITS", {"today_term": 10}),
        patch.object(config, "DEEPSEEK_WINDOW_CALL_LIMITS", {"today_main": 10}),
    ):
        runtime_config.return_value = {
            "enabled": True,
            "api_key": "test-key",
            "base_url": "https://example.invalid",
            "model": "deepseek-test",
            "pro_model": "deepseek-test-pro",
            "max_tokens": 800,
            "timeout_seconds": 12,
        }
        result = DeepSeekFeatureAnalysisService().analyze(
            "today_term",
            candidates,
            store,
            cutoff_at="2026-07-16T09:40:00",
            snapshot_id="snapshot-hard-cap",
            deadline_at="2026-07-16T10:00:00",
        )

    with store.repository.connect() as conn:
        sizes = [
            row[0]
            for row in conn.execute(
                "SELECT candidate_count FROM deepseek_analysis_batches WHERE api_called = 1 ORDER BY batch_id"
            ).fetchall()
        ]
    assert result["api_call_count"] == 2
    assert sizes == [8, 1]


def test_emergency_review_uses_reserved_budget_after_normal_deadline(tmp_path):
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = cls(2026, 7, 16, 15, 5)
            return value if tz is None else value.replace(tzinfo=tz)

    candidate = {
        "code": "600001",
        "score": 80,
        "pct_chg": 2.0,
        "speed": 0.4,
        "volume_ratio": 1.5,
        "turnover_rate": 4.0,
        "amplitude": 3.0,
    }
    store = StrategyValidationStore(str(tmp_path / "emergency.sqlite3"))

    def fake_validate(_parsed, *, strategy_name, candidates):
        item = candidates[0]
        return (
            [
                {
                    "code": item["code"],
                    "strategy": strategy_name,
                    "schema_version": FEATURE_SCHEMA_VERSION,
                    "evidence_hash": item["evidence_hash"],
                    "evidence_ids": [item["evidence"][0]["evidence_id"]],
                    "abstain": False,
                    "valid": True,
                }
            ],
            [],
        )

    with (
        patch("stock_analyzer.deepseek.feature_service.datetime", FrozenDateTime),
        patch("stock_analyzer.deepseek.feature_service.feature_runtime_config") as runtime_config,
        patch("stock_analyzer.deepseek.feature_service.validate_feature_response", side_effect=fake_validate),
        patch(
            "stock_analyzer.deepseek.feature_service.FEATURE_NEWS_CONTEXT_PROVIDER.attach",
            side_effect=lambda rows: rows,
        ),
        patch(
            "stock_analyzer.deepseek.feature_service.FEATURE_HTTP_CLIENT.post_json",
            return_value=DeepSeekHttpResult(parsed={"results": []}, raw={}, usage={}),
        ) as post_json,
        patch.object(config, "DEEPSEEK_FEATURE_CACHE_ENABLED", False),
        patch.object(config, "DEEPSEEK_DAILY_API_HARD_LIMIT", 10),
        patch.object(config, "DEEPSEEK_STRATEGY_CALL_LIMITS", {"emergency_reserved": 5}),
        patch.object(config, "DEEPSEEK_WINDOW_CALL_LIMITS", {"emergency_reserved": 5}),
    ):
        runtime_config.return_value = {
            "enabled": True,
            "api_key": "test-key",
            "base_url": "https://example.invalid",
            "model": "deepseek-test",
            "pro_model": "deepseek-test-pro",
            "max_tokens": 800,
            "timeout_seconds": 12,
        }
        result = DeepSeekFeatureAnalysisService().analyze(
            "tomorrow_picks",
            [candidate],
            store,
            cutoff_at="2026-07-16T15:05:00",
            deadline_at="2026-07-16T14:48:00",
            emergency=True,
        )

    summary = usage_summary(store, FrozenDateTime.now())
    assert result["status"] == "ok"
    assert result["api_call_count"] == 1
    assert post_json.call_count == 1
    assert summary["usage_by_window"]["emergency_reserved"] == 1


def test_feature_service_reserves_each_call_at_its_actual_start_time(tmp_path):
    class CrossingDateTime(datetime):
        calls = 0

        @classmethod
        def now(cls, tz=None):
            cls.calls += 1
            value = cls(2026, 7, 16, 9, 35 if cls.calls == 1 else 36)
            return value if tz is None else value.replace(tzinfo=tz)

    candidate = {
        "code": "600001",
        "score": 80,
        "pct_chg": 2.0,
        "speed": 0.4,
        "volume_ratio": 1.5,
        "turnover_rate": 4.0,
        "amplitude": 3.0,
    }
    captured = []

    def fake_reserve(_store, _batch_id, _strategy, requested_at, **_kwargs):
        captured.append(requested_at)
        return BudgetReservation(True, "reserved", "today_main", "today_term")

    with (
        patch("stock_analyzer.deepseek.feature_service.datetime", CrossingDateTime),
        patch("stock_analyzer.deepseek.feature_service.reserve_api_call", side_effect=fake_reserve),
        patch("stock_analyzer.deepseek.feature_service.feature_runtime_config") as runtime_config,
        patch("stock_analyzer.deepseek.feature_service.validate_feature_response", return_value=([], [])),
        patch(
            "stock_analyzer.deepseek.feature_service.FEATURE_NEWS_CONTEXT_PROVIDER.attach",
            side_effect=lambda rows: rows,
        ),
        patch(
            "stock_analyzer.deepseek.feature_service.FEATURE_HTTP_CLIENT.post_json",
            return_value=DeepSeekHttpResult(parsed={"results": []}, raw={}, usage={}),
        ),
        patch.object(config, "DEEPSEEK_FEATURE_CACHE_ENABLED", False),
    ):
        runtime_config.return_value = {
            "enabled": True,
            "api_key": "test-key",
            "base_url": "https://example.invalid",
            "model": "deepseek-test",
            "pro_model": "deepseek-test-pro",
            "max_tokens": 800,
            "timeout_seconds": 12,
        }
        DeepSeekFeatureAnalysisService().analyze(
            "today_term",
            [candidate],
            StrategyValidationStore(str(tmp_path / "crossing.sqlite3")),
            cutoff_at="2026-07-16T09:35:00",
            deadline_at="2026-07-16T10:00:00",
        )

    assert len(captured) == 1
    assert captured[0].strftime("%H:%M") == "09:36"
