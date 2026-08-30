from __future__ import annotations

import json
import sqlite3
import threading
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from trader.application.cadence import CadencePlannerStatus
from trader.application.decision_observers import DecisionObserverStatus
from trader.application.ports.runtime_status import V2InputQualityStatus, V2SupplyFunnel, V2SupplySummary
from trader.application.ports.v2_runtime import V2ResearchRuntimeStatus
from trader.application.v2_research_runtime import V2ResearchRuntime
from trader.bootstrap import (
    _initialize_reference_data_plane,
    _initialize_research_trace,
    build_system,
)
from trader.bootstrap_status import input_quality_payload, runtime_status
from trader.domain.recommendation.models import Strategy
from trader.infra.persistence.data_plane import DataPlaneRepository

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _config(tmp_path: Path) -> Path:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    runtime = json.loads((PROJECT_ROOT / "config/v2/runtime.json").read_text(encoding="utf-8"))
    runtime["runtime_dir"] = str(tmp_path / "runtime")
    runtime["strategy_config"] = str(PROJECT_ROOT / "config/v2/strategy.json")
    runtime["long_watchlist"] = str(PROJECT_ROOT / "config/v2/long_watchlist.json")
    path = config_dir / "runtime.json"
    path.write_text(json.dumps(runtime), encoding="utf-8")
    return path


def test_build_system_is_lazy_and_v2_only(tmp_path, monkeypatch) -> None:
    started: list[str] = []
    monkeypatch.setattr(threading.Thread, "start", lambda _thread: started.append("thread"))

    system = build_system(_config(tmp_path))

    assert started == []
    assert not (tmp_path / "runtime").exists()
    assert system.scheduler is not None
    assert system.repository is not None
    assert system.research_trace is not None
    assert not (tmp_path / "runtime" / "research").exists()
    assert system.long_v2_runtime is not None
    assert isinstance(system.scheduler._research, V2ResearchRuntime)
    assert system.scheduler.status().company_research.state == "stopped"
    assert system.app.test_client().get("/api/status").status_code == 404
    status = system.app.test_client().get("/api/v2/status")
    assert status.status_code == 200
    assert status.get_json()["phase"] == "closed"
    assert status.get_json()["tomorrow_model"]["active"] is True
    assert status.get_json()["tomorrow_model"]["model_id"] == "daily_reconstructible_ensemble_v1"
    assert status.get_json()["tomorrow_model"]["activation_basis"] == "manual_user_override"
    assert status.get_json()["tomorrow_model"]["monitoring_mode"] == "automatic_t1_outcome_settlement"
    assert status.get_json()["tomorrow_model"]["automatic_model_update"] is False
    page = system.app.test_client().get("/").get_data(as_text=True)
    assert 'name="trader-web-snapshot-retention-ms"' in page
    assert 'content="35000"' in page


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

    database = tmp_path / "v2-data" / "v2-data.sqlite3"
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
    scheduler.status.return_value = SimpleNamespace(
        running=True,
        phase=SimpleNamespace(value="afternoon"),
        config_version="runtime:test",
        lanes=(),
        hybrid_lanes=(),
        task_lanes=(),
        cadence=CadencePlannerStatus(None, {}, {}, {}, ()),
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
        company_research=V2ResearchRuntimeStatus(state="idle"),
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
    assert payload["scheduler"]["input_quality"] == {}
    assert payload["market_data"]["active_source"] == "sina"
    assert payload["market_data"]["market_feature_rows"] == 5567
    market_health.assert_called_once_with()


def test_runtime_status_serializes_typed_input_quality_for_web_cards() -> None:
    source_time = datetime(2026, 8, 24, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    status = V2InputQualityStatus(
        strategy=Strategy.TOMORROW,
        status="not_ready",
        publishable=False,
        summary=V2SupplySummary(
            trade_date=date(2026, 8, 24),
            quote_total_count=360,
            quote_covered_count=352,
            quote_missing_count=8,
            security_identity_missing_count=286,
            latest_quote_source="tencent",
            latest_quote_source_time=source_time,
            highest_final_score=74.25,
        ),
        supply_funnel=V2SupplyFunnel(
            requested_candidates=360,
            full_scored=65,
            filter_reject=216,
            observation_threshold_met_count=12,
            executable_threshold_met_count=3,
            selected_observe=2,
        ),
        candidate_count=360,
        candidate_feature_count=352,
        security_master_covered_count=74,
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
    assert payload["tomorrow"]["supply_funnel"]["full_scored"] == 65
    assert payload["tomorrow"]["supply_funnel"]["observation_threshold_met_count"] == 12
    assert payload["tomorrow"]["supply_funnel"]["executable_threshold_met_count"] == 3
    assert payload["tomorrow"]["candidate_optional_reason_counts"] == {
        "missing_listing_age_sessions": 65,
        "missing_listing_date": 221,
    }
    assert "asdict(status.supply_funnel)" not in (PROJECT_ROOT / "src/trader/bootstrap_status.py").read_text(
        encoding="utf-8"
    )


def test_bootstrap_wires_real_outcome_settlement_without_eager_database_write(tmp_path) -> None:
    system = build_system(_config(tmp_path))

    assert type(system.scheduler._dependencies.settlement).__name__ == "V2OutcomeSettlementAdapter"
    assert not (tmp_path / "runtime" / "research" / "outcomes.sqlite3").exists()
