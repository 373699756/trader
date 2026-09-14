from __future__ import annotations

import json
import sqlite3
import threading
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from trader.application.decisions.decision_observers import DecisionObserverStatus
from trader.application.market_data.input_runtime import MarketDataAdapter
from trader.application.ports.runtime_status import (
    InputQualityStatus,
    SupplySummary,
)
from trader.application.ports.scheduler import ResearchRuntimeStatus
from trader.application.research.research_runtime import ResearchRuntime
from trader.application.runtime.cadence import (
    CadencePlannerStatus,
    SchedulePointKey,
    SchedulePointState,
    SchedulePointStatus,
)
from trader.application.runtime.schedule import SchedulePoint
from trader.application.runtime.scheduler_runtime import TradingCalendarRuntimeStatus
from trader.bootstrap import (
    _initialize_reference_data_plane,
    _initialize_research_trace,
    build_system,
)
from trader.bootstrap_status import input_quality_payload, runtime_status
from trader.domain.recommendation.models import Strategy
from trader.domain.recommendation.pipeline import (
    PipelineFacet,
    PipelineMetricRange,
    PipelineStageStatus,
    RecommendationPipelineStatus,
)
from trader.infra.persistence.data_plane import DataPlaneRepository

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_recommendation_pipeline_requires_every_canonical_stage() -> None:
    with pytest.raises(ValueError, match="pipeline stages are invalid"):
        RecommendationPipelineStatus(
            current_stage="dynamic_filter",
            stages=(PipelineStageStatus("dynamic_filter", "completed", 1, 1),),
        )


def _config(tmp_path: Path) -> Path:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    strategy = json.loads((PROJECT_ROOT / "config/strategy.json").read_text(encoding="utf-8"))
    strategy["scoring_profile"] = "v1"
    strategy_path = config_dir / "strategy.json"
    strategy_path.write_text(json.dumps(strategy), encoding="utf-8")
    runtime = json.loads((PROJECT_ROOT / "config/runtime.json").read_text(encoding="utf-8"))
    runtime["runtime_dir"] = str(tmp_path / "runtime")
    runtime["strategy_config"] = str(strategy_path)
    runtime["long_watchlist"] = str(PROJECT_ROOT / "config/long_watchlist.json")
    path = config_dir / "runtime.json"
    path.write_text(json.dumps(runtime), encoding="utf-8")
    return path


def _config_with_strategy_profile(tmp_path: Path, profile: str) -> Path:
    strategy = json.loads((PROJECT_ROOT / "config/strategy.json").read_text(encoding="utf-8"))
    strategy["scoring_profile"] = profile
    strategy_path = tmp_path / "strategy.json"
    strategy_path.write_text(json.dumps(strategy), encoding="utf-8")
    path = _config(tmp_path)
    runtime = json.loads(path.read_text(encoding="utf-8"))
    runtime["strategy_config"] = str(strategy_path)
    path.write_text(json.dumps(runtime), encoding="utf-8")
    return path


def test_build_system_is_lazy_and_current_only(tmp_path, monkeypatch) -> None:
    started: list[str] = []
    monkeypatch.setattr(threading.Thread, "start", lambda _thread: started.append("thread"))

    system = build_system(_config(tmp_path))

    assert started == []
    assert not (tmp_path / "runtime").exists()
    assert system.scheduler is not None
    assert system.repository is not None
    assert system.research_trace is not None
    assert system.quote_pool.status().workers == 4
    assert system.quote_pool.is_running() is False
    assert system.scheduler._dependencies.data._market.quotes.gateway._tencent._worker_pool is system.quote_pool
    assert not (tmp_path / "runtime" / "research").exists()
    assert system.long_runtime is not None
    assert isinstance(system.scheduler._research, ResearchRuntime)
    assert system.scheduler.status().company_research.state == "stopped"
    assert system.app.test_client().get("/api/v2/status").status_code == 404
    status = system.app.test_client().get("/api/status")
    assert status.status_code == 200
    assert status.get_json()["phase"] == "closed"
    scoring = status.get_json()["scoring_profile"]
    assert scoring["profile_id"] == "v1"
    assert set(scoring["heads"]) == {"tomorrow"}
    tomorrow = scoring["heads"]["tomorrow"]
    assert tomorrow["active"] is True
    assert tomorrow["profile_id"] == "v1"
    assert tomorrow["model_id"] == "residual_momentum_linear"
    assert tomorrow["activation_basis"] == "manual_user_override"
    assert tomorrow["monitoring_mode"] == "automatic_t1_outcome_settlement"
    assert tomorrow["automatic_model_update"] is False
    assert tomorrow["computation"] == {
        "cache_hit_count": 0,
        "candidate_count": 0,
        "computed_groups": [],
        "deadline_abandon_reason": None,
        "decision_age_ms": 0.0,
        "predictor_batch_count": 0,
        "request_count": 0,
        "reused_groups": [],
        "stage_durations": [],
    }
    assert "tomorrow_profile_comparison" not in status.get_json()
    page = system.app.test_client().get("/").get_data(as_text=True)
    assert 'name="trader-web-snapshot-retention-ms"' in page
    assert 'content="35000"' in page


def test_build_system_wires_history_completion_to_scoring_refresh(tmp_path, monkeypatch) -> None:
    system = build_system(_config(tmp_path))
    native_data = system.scheduler._dependencies.data
    assert isinstance(native_data, MarketDataAdapter)
    calls: list[str] = []
    monkeypatch.setattr(native_data, "invalidate_history", lambda: calls.append("invalidate"))
    monkeypatch.setattr(system.scheduler, "notify_history_warmup", lambda: calls.append("schedule"))

    native_data._market.warmup._on_batch_complete(("600001",))  # noqa: SLF001 - composition-root wiring contract

    assert calls == ["invalidate", "schedule"]


def test_build_system_selects_an_explicit_scoring_profile_without_rewriting_config(tmp_path, monkeypatch) -> None:
    from trader.infra.scoring.profile_factory import load_scoring_profile

    monkeypatch.setattr(threading.Thread, "start", lambda _thread: None)

    config_path = _config_with_strategy_profile(tmp_path, "v1")
    strategy_path = Path(json.loads(config_path.read_text(encoding="utf-8"))["strategy_config"])
    original = strategy_path.read_bytes()
    monkeypatch.setattr(
        "trader.bootstrap.load_scoring_profile",
        lambda profile, *, training_root: load_scoring_profile(profile, training_root=PROJECT_ROOT / "data" / "train"),
    )
    system = build_system(config_path, scoring_profile="v2")
    status = system.app.test_client().get("/api/status").get_json()["scoring_profile"]

    assert status["profile_id"] == "v2"
    assert set(status["heads"]) == {"today", "tomorrow", "d25"}
    assert all(head["active"] is True for head in status["heads"].values())
    assert status["heads"]["tomorrow"]["model_id"] == "industry_ridge_lightgbm"
    assert status["heads"]["tomorrow"]["activation_basis"] == "manual_user_override"
    assert strategy_path.read_bytes() == original


def test_build_system_passes_project_training_root_for_v3(tmp_path, monkeypatch) -> None:
    from trader.infra.scoring.profile_factory import load_scoring_profile

    observed: list[Path] = []
    v1_profile = load_scoring_profile("v1")

    def load(profile: str, *, training_root: Path | None = None):
        observed.append(training_root or Path())
        return v1_profile

    monkeypatch.setattr("trader.bootstrap.load_scoring_profile", load)

    build_system(_config_with_strategy_profile(tmp_path, "v3"))

    assert observed == [tmp_path / "data" / "train"]


def test_reference_data_plane_recovery_is_fail_open() -> None:
    from unittest.mock import Mock

    market_data = Mock()
    data_plane = Mock()
    market_data.research = Mock()
    market_data.references.recover.side_effect = RuntimeError("recover failed")
    data_plane.initialize.side_effect = RuntimeError("db unavailable")

    _initialize_reference_data_plane(market_data, data_plane)

    data_plane.initialize.assert_called_once_with()
    market_data.references.recover.assert_not_called()
    market_data.research.recover_from_data_plane.assert_not_called()


def test_reference_data_plane_recovery_schedules_official_security_master_on_startup() -> None:
    from unittest.mock import Mock

    observed_at = datetime(2026, 8, 30, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    market_data = Mock()
    data_plane = Mock()

    _initialize_reference_data_plane(market_data, data_plane, observed_at)

    market_data.references.recover.assert_called_once_with()
    market_data.references.schedule_security_master_refresh.assert_called_once_with(observed_at)


def test_reference_data_plane_physical_corruption_is_fail_open(tmp_path: Path) -> None:
    from unittest.mock import Mock

    database = tmp_path / "market-data" / "market-data.sqlite3"
    database.parent.mkdir(parents=True)
    database.write_bytes(b"not-a-sqlite-database")
    market_data = Mock()

    _initialize_reference_data_plane(market_data, DataPlaneRepository(tmp_path))

    market_data.references.recover.assert_not_called()
    market_data.history.recover_from_data_plane.assert_not_called()
    market_data.research.recover_from_data_plane.assert_not_called()


def test_research_trace_initialization_is_fail_open() -> None:
    from unittest.mock import Mock

    trace = Mock()
    trace.initialize.side_effect = sqlite3.OperationalError("research database unavailable")

    _initialize_research_trace(trace)

    trace.initialize.assert_called_once_with()


def test_runtime_status_exposes_and_degrades_on_research_observer_failure() -> None:
    from unittest.mock import Mock

    scheduler = Mock()
    point_updated_at = datetime(2026, 8, 31, 11, 20, tzinfo=ZoneInfo("Asia/Shanghai"))
    point_key = SchedulePointKey("2026-08-31", SchedulePoint.TODAY_FREEZE, "today")
    scheduler.status.return_value = SimpleNamespace(
        running=True,
        phase=SimpleNamespace(value="afternoon"),
        config_version="runtime:test",
        lanes=(),
        hybrid_lanes=(),
        task_lanes=(),
        cadence=CadencePlannerStatus(
            None,
            {},
            {},
            {point_key: SchedulePointState(SchedulePointStatus.COMPLETED, 1, point_updated_at)},
            (),
        ),
        observer=DecisionObserverStatus(
            capacity=16,
            accepting=True,
            thread_alive=True,
            running=False,
            depth=0,
            accepted_count=10,
            rejected_count=0,
            completed_count=10,
            consumer_failure_count=3,
            last_error_code="ResearchTraceCapacityError",
        ),
        company_research=ResearchRuntimeStatus(state="idle"),
        strategy_error_codes=(),
        recent_errors=(),
        deepseek=Mock(),
        control_running=True,
        control_inflight=0,
        control_rejected_count=0,
        refresh_failure_count=0,
        decision_failure_count=0,
        review_failure_count=0,
        overlay_publish_count=0,
        overlay_failure_count=0,
        local_publish_count=1,
        hybrid_publish_count=0,
        publish_rejection_count=0,
        observer_rejection_count=0,
        freeze_completed_count=0,
        freeze_failure_count=0,
        settlement_completed_count=0,
        settlement_failure_count=0,
        last_error_code="",
        input_quality=(),
        calendar=TradingCalendarRuntimeStatus(
            state="unknown",
            trade_date=None,
            is_trading_day=None,
            consecutive_failure_count=0,
            next_retry_at=None,
        ),
    )
    reviewer = Mock()
    reviewer.status.return_value = {
        "status": "ready",
        "budget": {"limit": 168, "used": 0, "remaining": 168},
    }
    market_health = Mock(
        return_value={
            "active_source": "sina",
            "market_feature_rows": 5567,
            "market_quote_age": {"sample_count": 5567, "latest_source_time": "2026-08-24T09:40:00+08:00"},
        }
    )

    payload = runtime_status(scheduler, reviewer, market_health)

    assert payload["observer"]["consumer_failure_count"] == 3
    assert payload["company_research"]["state"] == "idle"
    assert payload["company_research"]["completed_batches"] == 0
    assert payload["health"] == {"level": "degraded", "issue_count": 1}
    assert payload["degraded_reasons"] == ["observer:ResearchTraceCapacityError"]
    assert payload["last_error"] == "observer:ResearchTraceCapacityError"
    assert payload["scheduler"]["settlement_completed_count"] == 0
    assert payload["scheduler"]["settlement_failure_count"] == 0
    assert payload["scheduler"]["overlay_publish_count"] == 0
    assert payload["scheduler"]["overlay_failure_count"] == 0
    assert payload["scheduler"]["hybrid_lanes"] == []
    assert payload["scheduler"]["cadence"]["started_at"] is None
    assert payload["scheduler"]["calendar"] == {
        "state": "unknown",
        "trade_date": None,
        "is_trading_day": None,
        "consecutive_failure_count": 0,
        "next_retry_at": None,
    }
    assert payload["scheduler"]["cadence"]["schedule_points"] == [
        {
            "trade_date": "2026-08-31",
            "schedule_point": "today_freeze",
            "strategy": "today",
            "status": "completed",
            "attempt_count": 1,
            "updated_at": point_updated_at.isoformat(),
            "next_retry_at": None,
        }
    ]
    assert payload["scheduler"]["input_quality"] == {}
    assert payload["market_data"]["active_source"] == "sina"
    assert payload["market_data"]["market_feature_rows"] == 5567
    market_health.assert_called_once_with()


def test_runtime_status_serializes_typed_input_quality_for_web_cards() -> None:
    source_time = datetime(2026, 8, 24, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    status = InputQualityStatus(
        strategy=Strategy.TOMORROW,
        status="not_ready",
        publishable=False,
        summary=SupplySummary(
            trade_date=date(2026, 8, 24),
            quote_total_count=360,
            quote_covered_count=352,
            quote_missing_count=8,
            security_identity_missing_count=286,
            latest_quote_source="tencent",
            latest_quote_source_time=source_time,
            highest_final_score=74.25,
        ),
        pipeline=RecommendationPipelineStatus(
            current_stage="action_gate",
            stages=(
                PipelineStageStatus("dynamic_filter", "completed", 5200, 4800),
                PipelineStageStatus("board_cross_section", "completed", 4800, 4800),
                PipelineStageStatus("strategy_history", "completed", 4800, 4300),
                PipelineStageStatus("model_input", "completed", 4300, 4200),
                PipelineStageStatus(
                    "candidate_score",
                    "completed",
                    4200,
                    900,
                    metric_ranges=(PipelineMetricRange("candidate_score", 48.5, 82.25),),
                    threshold=50.0,
                ),
                PipelineStageStatus("board_limit", "completed", 900, 360),
                PipelineStageStatus("candidate_refresh", "completed", 360, 352),
                PipelineStageStatus(
                    "input_coverage",
                    "degraded",
                    360,
                    None,
                    facets=(
                        PipelineFacet("candidate_features", 352, 360),
                        PipelineFacet("security_master", 74, 360),
                        PipelineFacet("history", 352, 360),
                    ),
                ),
                PipelineStageStatus(
                    "evidence_score",
                    "completed",
                    352,
                    65,
                    metric_ranges=(PipelineMetricRange("base_score", 44.5, 74.25),),
                ),
                PipelineStageStatus("model_cost_gate", "completed", 65, 54),
                PipelineStageStatus("local_score", "completed", 65, 65),
                PipelineStageStatus("deepseek_review", "pending", 20, None),
                PipelineStageStatus("fusion", "completed", 65, 65),
                PipelineStageStatus(
                    "action_gate",
                    "completed",
                    65,
                    2,
                    facets=(
                        PipelineFacet("executable_threshold_met", 3, 65),
                        PipelineFacet("observation_threshold_met", 12, 65),
                        PipelineFacet("action_executable", 0, 65),
                        PipelineFacet("action_observe", 2, 65),
                        PipelineFacet("action_unavailable", 63, 65),
                    ),
                ),
                PipelineStageStatus(
                    "concentration",
                    "completed",
                    2,
                    2,
                    facets=(PipelineFacet("selected_observe", 2, 2),),
                ),
            ),
        ),
        candidate_count=360,
        candidate_feature_count=352,
        security_master_covered_count=74,
        history_required_sessions=61,
        candidate_feature_coverage_ratio=352 / 360,
        security_master_coverage_ratio=74 / 360,
        candidate_optional_reason_counts=(("missing_listing_date", 221), ("missing_listing_age_sessions", 65)),
        primary_blocker="security_master_coverage_incomplete",
    )

    payload = input_quality_payload((status,))

    assert payload["tomorrow"]["summary"] == {
        "trade_date": "2026-08-24",
        "quote_total_count": 360,
        "quote_covered_count": 352,
        "quote_missing_count": 8,
        "security_identity_missing_count": 286,
        "latest_quote_source": "tencent",
        "latest_quote_source_time": source_time.isoformat(),
        "highest_final_score": 74.25,
    }
    pipeline = payload["tomorrow"]["pipeline"]
    assert pipeline["current_stage"] == "action_gate"
    stages = {stage["key"]: stage for stage in pipeline["stages"]}
    assert stages["dynamic_filter"]["input_count"] == 5200
    assert stages["dynamic_filter"]["output_count"] == 4800
    assert stages["candidate_refresh"]["output_count"] == 352
    assert stages["evidence_score"]["output_count"] == 65
    assert stages["candidate_score"]["metric_ranges"] == [
        {"metric": "candidate_score", "minimum": 48.5, "maximum": 82.25}
    ]
    assert payload["tomorrow"]["history_required_sessions"] == 61
    action_facets = {facet["key"]: facet for facet in stages["action_gate"]["facets"]}
    assert action_facets["observation_threshold_met"]["count"] == 12
    assert action_facets["executable_threshold_met"]["count"] == 3
    assert payload["tomorrow"]["candidate_optional_reason_counts"] == {
        "missing_listing_age_sessions": 65,
        "missing_listing_date": 221,
    }
    assert "supply_funnel" not in payload["tomorrow"]
    assert "asdict(status.pipeline)" not in (PROJECT_ROOT / "src/trader/bootstrap_status.py").read_text(
        encoding="utf-8"
    )


def test_bootstrap_wires_real_outcome_settlement_without_eager_database_write(tmp_path) -> None:
    system = build_system(_config(tmp_path))

    assert type(system.scheduler._dependencies.settlement).__name__ == "OutcomeSettlementAdapter"
    assert not (tmp_path / "runtime" / "research" / "outcomes.sqlite3").exists()
