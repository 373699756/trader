from __future__ import annotations

import json
import shutil
import sqlite3
import threading
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from tests.unit.training.infra.profile.v3.test_profile import _publish_profile
from trader.bootstrap import BuildSystem
from trader.infra.initialization import InitializeIfAvailable
from trader.recommendation.application.pipeline.data_source.source_router import MarketInputCoordinator
from trader.recommendation.application.pipeline.freeze_publish.freeze_observers import FreezeObserverStatus
from trader.recommendation.application.ports.market_data import MarketSnapshotMetadata
from trader.recommendation.application.ports.read_only_queries import (
    InputQualityStatus,
    SupplySummary,
)
from trader.recommendation.application.ports.runtime import ResearchRuntimeStatus
from trader.recommendation.application.runtime.cadence import (
    CadencePlannerStatus,
    SchedulePointKey,
    SchedulePointState,
    SchedulePointStatus,
)
from trader.recommendation.application.runtime.schedule import SchedulePoint
from trader.recommendation.application.runtime.scheduler_runtime import TradingCalendarRuntimeStatus
from trader.recommendation.domain.evidence.pipeline import (
    PipelineStage,
    PipelineStageSnapshot,
    PipelineStageStatus,
    RecommendationPipelineStatus,
    SourceHealth,
    SourceHealthState,
    StageState,
    ValidateStageBatchContinuity,
)
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.domain.publication.preview import PreviewContext
from trader.recommendation.infra.market_data.reference_initialization import InitializeMarketReferences
from trader.recommendation.infra.status_projection import InputQualityPayload, RuntimeStatus
from trader.training.application.research_runtime import ResearchRuntime
from trader.training.infra.profile.v2.contracts import V2_TRAINING_PROFILE
from trader.training.infra.profile.v3.contracts import V3_TRAINING_PROFILE

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_recommendation_pipeline_requires_every_canonical_stage() -> None:
    with pytest.raises(ValueError, match="pipeline stages are invalid"):
        RecommendationPipelineStatus(
            current_stage="dynamic_filter",
            stages=(PipelineStageStatus("dynamic_filter", "completed", 1, 1),),
        )


def test_missing_market_snapshot_metadata_is_explicitly_representable() -> None:
    metadata = MarketSnapshotMetadata()
    assert metadata.status == "missing"
    assert metadata.merge_epoch == ""

    with pytest.raises(ValueError, match="merge_epoch"):
        MarketSnapshotMetadata(status="stale")


def test_pipeline_batch_continuity_requires_matching_counts() -> None:
    observed_at = datetime(2026, 8, 24, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    source_health = SourceHealth(SourceHealthState.READY, 1, 1, observed_at, 0.0)
    stages = tuple(
        PipelineStageSnapshot(
            stage=stage,
            stage_order=index,
            input_batch_id=f"pipeline:{index - 1}",
            output_batch_id=f"pipeline:{index}",
            as_of=observed_at,
            state=StageState.READY,
            input_count=1,
            output_count=1,
            rejected_count=0,
            pending_count=0,
            failed_count=0,
            reasons=(),
            source_health=source_health,
            latency_ms=1,
            degraded=False,
        )
        for index, stage in enumerate(PipelineStage, start=1)
    )
    with pytest.raises(ValueError, match="counts are not continuous"):
        ValidateStageBatchContinuity((*stages[:3], replace(stages[3], input_count=2, pending_count=1), *stages[4:]))


def _config(tmp_path: Path) -> Path:
    config_dir = tmp_path / "config"
    shutil.copytree(PROJECT_ROOT / "config", config_dir, dirs_exist_ok=True)
    strategy = json.loads((config_dir / "strategy.json").read_text(encoding="utf-8"))
    strategy["scoring_profile"] = "v2"
    strategy_path = config_dir / "strategy.json"
    strategy_path.write_text(json.dumps(strategy), encoding="utf-8")
    runtime = json.loads((config_dir / "runtime.json").read_text(encoding="utf-8"))
    runtime["runtime_dir"] = str(tmp_path / "runtime")
    runtime["strategy_config"] = str(strategy_path)
    runtime["long_watchlist"] = str(config_dir / "long_watchlist.json")
    reference_dir = tmp_path / "data/market/reference"
    reference_dir.mkdir(parents=True, exist_ok=True)
    for profile in (V2_TRAINING_PROFILE, V3_TRAINING_PROFILE):
        _publish_profile(tmp_path / "data" / "training" / "models", profile)
    path = config_dir / "runtime.json"
    path.write_text(json.dumps(runtime), encoding="utf-8")
    return path


def _config_with_strategy_profile(tmp_path: Path, profile: str) -> Path:
    path = _config(tmp_path)
    strategy_path = path.parent / "strategy.json"
    strategy = json.loads(strategy_path.read_text(encoding="utf-8"))
    strategy["scoring_profile"] = profile
    strategy_path.write_text(json.dumps(strategy), encoding="utf-8")
    return path


def test_build_system_is_lazy_and_current_only(tmp_path, monkeypatch) -> None:
    started: list[str] = []
    monkeypatch.setattr(threading.Thread, "start", lambda _thread: started.append("thread"))

    system = BuildSystem(_config(tmp_path))

    assert started == []
    assert not (tmp_path / "runtime").exists()
    assert not (tmp_path / "data/recommendation").exists()
    assert system.scheduler is not None
    assert system.records is not None
    assert system.records._database == tmp_path / "data/recommendation/v2/freeze.sqlite3"  # noqa: SLF001
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
    assert scoring["profile_id"] == "v2"
    assert set(scoring["heads"]) == {"tomorrow", "d25"}
    tomorrow = scoring["heads"]["tomorrow"]
    assert tomorrow["is_enable"] is True
    assert "active" not in tomorrow
    assert tomorrow["profile_id"] == "v2"
    assert tomorrow["model_id"] == "v2_industry_lightgbm"
    assert tomorrow["activation_basis"] == "manual_user_override"
    assert tomorrow["runtime_anchor"] == "15:00"
    assert scoring["heads"]["d25"]["runtime_anchor"] == "15:00"
    assert tomorrow["monitoring_mode"] == "automatic_t1_outcome_settlement"
    assert tomorrow["automatic_model_update"] is False
    assert tomorrow["computation"] == {
        "cache_hit_count": 0,
        "candidate_count": 0,
        "computed_groups": [],
        "deadline_abandon_reason": None,
        "freeze_age_ms": 0.0,
        "predictor_batch_count": 0,
        "request_count": 0,
        "reused_groups": [],
        "stage_durations": [],
    }
    assert "tomorrow_profile_comparison" not in status.get_json()
    page = system.app.test_client().get("/").get_data(as_text=True)
    assert 'name="trader-web-snapshot-retention-ms"' in page
    assert 'content="35000"' in page


def test_deepseek_toggle_rebuilds_with_new_configuration_identity_and_readable_status(tmp_path, monkeypatch) -> None:
    config_path = _config(tmp_path)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fixture-deepseek-key")
    default = BuildSystem(config_path)
    assert default.settings.deepseek.enabled is False
    assert default.settings.deepseek.api_key == ""
    default_payload = default.app.test_client().get("/api/status").get_json()
    assert default_payload["deepseek"]["enabled"] is False
    assert default_payload["deepseek"]["physical_attempts"] == 0
    assert default_payload["deepseek"]["zero_call_reason"] == "disabled"

    deepseek_path = config_path.parent / "deepseek.json"
    document = json.loads(deepseek_path.read_text(encoding="utf-8"))
    document["deepseek"]["enabled"] = True
    deepseek_path.write_text(json.dumps(document), encoding="utf-8")
    enabled = BuildSystem(config_path)
    assert enabled.settings.deepseek.enabled is True
    assert enabled.settings.config_version != default.settings.config_version
    enabled_response = enabled.app.test_client().get("/api/status")
    assert enabled_response.status_code == 200
    assert enabled_response.get_json()["deepseek"]["enabled"] is True

    deepseek_path = config_path.parent / "deepseek.json"
    document = json.loads(deepseek_path.read_text(encoding="utf-8"))
    document["deepseek"]["enabled"] = False
    deepseek_path.write_text(json.dumps(document), encoding="utf-8")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY_FILE", str(tmp_path / "missing-deepseek-key"))

    disabled = BuildSystem(config_path)

    assert disabled.settings.config_version != enabled.settings.config_version
    assert disabled.settings.deepseek.enabled is False
    response = disabled.app.test_client().get("/api/status")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["deepseek"]["enabled"] is False
    assert payload["deepseek"]["physical_attempts"] == 0
    assert payload["deepseek"]["zero_call_reason"] == "disabled"
    assert not (tmp_path / "runtime").exists()
    assert not (tmp_path / "data/recommendation").exists()


def test_freeze_index_initializes_only_under_the_freeze_directory(tmp_path) -> None:
    system = BuildSystem(_config(tmp_path))

    system.records.initialize()

    freeze_root = tmp_path / "data/recommendation/v2"
    assert {path.name for path in freeze_root.iterdir() if path.suffix == ".sqlite3"} == {"freeze.sqlite3"}
    assert not (tmp_path / "runtime" / "freezes").exists()


def test_build_system_reads_the_same_history_owned_by_download(tmp_path) -> None:
    from trader.download.infra.download_settings import LoadHistorySyncConfiguration
    from trader.recommendation.infra.market_data.published_history_cache import PublishedHistoryCache

    system = BuildSystem(_config(tmp_path))
    native_data = system.scheduler._dependencies.data
    assert isinstance(native_data, MarketInputCoordinator)
    assert isinstance(native_data._market.history, PublishedHistoryCache)  # noqa: SLF001
    assert system.input_preloader._profiles[0][0] is native_data._market.history  # noqa: SLF001
    configuration = LoadHistorySyncConfiguration(system.settings.project_root, system.settings.data_paths)
    assert native_data._market.history._history.publication._root == system.settings.data_paths.history  # noqa: SLF001
    assert native_data._market.history._outcome_history.publication._root == configuration.history_root  # noqa: SLF001


@pytest.mark.parametrize("hour,minute", ((8, 0), (10, 0), (12, 0), (14, 59), (15, 10), (20, 30)))
def test_startup_input_preloader_never_downloads_and_keeps_web_readable(
    tmp_path,
    monkeypatch,
    hour,
    minute,
) -> None:
    def unexpected_download(*_args, **_kwargs):
        pytest.fail("server startup must not construct a supplier or download")

    monkeypatch.setattr(
        "trader.download.infra.baostock_sync_supplier.BaoStockHistorySupplier.__init__", unexpected_download
    )
    monkeypatch.setattr(
        "trader.download.application.download_history.DownloadHistoryUseCase.execute", unexpected_download
    )
    monkeypatch.setattr("trader.bootstrap.ExecuteSelectedTraining", unexpected_download)
    now = datetime(2026, 10, 9, hour, minute, tzinfo=ZoneInfo("Asia/Shanghai"))
    monkeypatch.setattr("trader.bootstrap._utc_now", lambda: now)
    system = BuildSystem(_config(tmp_path))
    preloader = system.input_preloader
    assert preloader.start() is True
    try:
        assert preloader._thread is not None
        preloader._thread.join(2)
        assert not preloader._thread.is_alive()
        client = system.app.test_client()
        assert client.get("/").status_code == 200
        with monkeypatch.context() as scope:
            scope.setattr(sqlite3, "connect", lambda *args, **kwargs: pytest.fail("status reread a database"))
            scope.setattr(Path, "read_bytes", lambda *args, **kwargs: pytest.fail("status reread a file"))
            scope.setattr(Path, "read_text", lambda *args, **kwargs: pytest.fail("status reread a file"))
            payload = client.get("/api/status").get_json()
        assert payload["market_data"]["published_history_snapshot_hash"] is None
        assert len(payload["scoring_profile"]["maintenance"]) == 4
        assert not (tmp_path / "data/history").exists()
    finally:
        assert preloader.stop(wait=True).completed is True
    assert preloader.start() is False


def test_build_system_selects_an_explicit_scoring_profile_without_rewriting_config(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(threading.Thread, "start", lambda _thread: None)

    config_path = _config_with_strategy_profile(tmp_path, "v2")
    strategy_path = Path(json.loads(config_path.read_text(encoding="utf-8"))["strategy_config"])
    original = strategy_path.read_bytes()
    system = BuildSystem(config_path, scoring_profile="v2")
    status = system.app.test_client().get("/api/status").get_json()["scoring_profile"]

    assert status["profile_id"] == "v2"
    assert set(status["heads"]) == {"tomorrow", "d25"}
    assert all(head["is_enable"] is True for head in status["heads"].values())
    assert status["heads"]["tomorrow"]["model_id"] == "v2_industry_lightgbm"
    assert status["heads"]["tomorrow"]["activation_basis"] == "manual_user_override"
    assert strategy_path.read_bytes() == original


def test_build_system_passes_project_training_root_for_v3(tmp_path, monkeypatch) -> None:
    from trader.recommendation.infra.scoring.profile_factory import LoadScoringProfile

    observed: list[Path] = []
    config_path = _config_with_strategy_profile(tmp_path, "v3")
    v3_profile = LoadScoringProfile("v3", training_root=tmp_path / "data" / "training" / "models")

    def load(profile: str, *, training_root: Path | None = None):
        assert profile == "v3"
        observed.append(training_root or Path())
        return v3_profile

    monkeypatch.setattr("trader.bootstrap.LoadScoringProfile", load)

    BuildSystem(config_path)

    assert observed == [tmp_path / "data" / "training" / "models"]


def test_published_reference_initialization_updates_quote_merge() -> None:
    from unittest.mock import Mock

    market_data = Mock()

    InitializeMarketReferences(market_data)

    market_data.references.initialize.assert_called_once_with()
    market_data.quotes.gateway.update_reference_observations.assert_called_once_with(
        market_data.references.security_observations.return_value
    )


def test_research_trace_initialization_is_fail_open() -> None:
    from unittest.mock import Mock

    trace = Mock()
    trace.initialize.side_effect = sqlite3.OperationalError("research database unavailable")

    assert InitializeIfAvailable(trace) is False

    trace.initialize.assert_called_once_with()


@pytest.mark.parametrize("preview", (False, True))
def test_runtime_status_exposes_and_degrades_on_research_observer_failure(preview) -> None:
    from unittest.mock import Mock

    scheduler = Mock()
    point_updated_at = datetime(2026, 8, 31, 14, 50, tzinfo=ZoneInfo("Asia/Shanghai"))
    point_key = SchedulePointKey("2026-08-31", SchedulePoint.AFTERNOON_FREEZE, "tomorrow")
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
        observer=FreezeObserverStatus(
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
        session=SimpleNamespace(generation=0, discontinuity_reason="startup"),
        strategy_error_codes=(),
        recent_errors=(),
        deepseek=Mock(),
        control_running=True,
        control_inflight=0,
        control_rejected_count=0,
        refresh_failure_count=0,
        freeze_failure_count=0,
        review_failure_count=0,
        overlay_publish_count=0,
        overlay_failure_count=0,
        local_publish_count=1,
        hybrid_publish_count=0,
        publish_rejection_count=0,
        observer_rejection_count=0,
        freeze_completed_count=0,
        seal_failure_count=0,
        settlement_completed_count=0,
        settlement_failure_count=0,
        last_error_code="",
        input_quality=(),
        publication_io=(),
        preview_context=PreviewContext("status-preview", date(2026, 8, 28), point_updated_at) if preview else None,
        preview_state="failed" if preview else None,
        preview_error="production_model_contract_conflict" if preview else None,
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
            "latest_source": "sina",
            "market_feature_rows": 5567,
            "market_quote_age": {"sample_count": 5567, "latest_source_time": "2026-08-24T09:40:00+08:00"},
        }
    )

    payload = RuntimeStatus(scheduler, reviewer, market_health)

    if preview:
        assert payload["run_mode"] == "preview"
        assert payload["market_trade_date"] == "2026-08-28"
        assert payload["market_as_of"] == "2026-08-28T15:00:00+08:00"
        assert payload["preview_started_at"] == point_updated_at.isoformat()
        assert payload["preview_state"] == "failed"
        assert payload["preview_error"] == "production_model_contract_conflict"
    else:
        assert {"run_mode", "market_trade_date", "market_as_of", "preview_state", "preview_error"}.isdisjoint(payload)
    assert payload["observer"]["consumer_failure_count"] == 3
    assert payload["company_research"]["state"] == "idle"
    assert payload["health"] == {"level": "degraded", "issue_count": 2 if preview else 1}
    assert payload["degraded_reasons"] == [
        "observer:ResearchTraceCapacityError",
        *(["preview:production_model_contract_conflict"] if preview else []),
    ]
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
        "generation": 0,
        "discontinuity_reason": "startup",
    }
    assert payload["scheduler"]["cadence"]["schedule_points"] == [
        {
            "trade_date": "2026-08-31",
            "schedule_point": "afternoon_freeze",
            "strategy": "tomorrow",
            "status": "completed",
            "attempt_count": 1,
            "updated_at": point_updated_at.isoformat(),
            "next_retry_at": None,
        }
    ]
    assert payload["scheduler"]["input_quality"] == {}
    assert payload["market_data"]["latest_source"] == "sina"
    assert payload["market_data"]["market_feature_rows"] == 5567
    market_health.assert_called_once_with()


def test_runtime_status_serializes_typed_input_quality_for_web_cards() -> None:
    source_time = datetime(2026, 8, 24, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    stage_snapshots = tuple(
        PipelineStageSnapshot(
            stage=stage,
            stage_order=index,
            input_batch_id="pipeline:test",
            output_batch_id="pipeline:test",
            as_of=source_time,
            state=StageState.READY,
            input_count=1,
            output_count=1,
            rejected_count=0,
            pending_count=0,
            failed_count=0,
            reasons=(),
            source_health=SourceHealth(SourceHealthState.READY, 1, 1, source_time, 0.0),
            latency_ms=1,
            degraded=False,
        )
        for index, stage in enumerate(PipelineStage, start=1)
    )
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
            current_stage="action_publish",
            stages=tuple(
                PipelineStageStatus(
                    stage.value,
                    "completed",
                    1,
                    1,
                    duration_ms=135,
                    rejected_count=0,
                    pending_count=0,
                    failed_count=0,
                    not_selected_count=0,
                )
                for stage in PipelineStage
            ),
            stage_snapshots=stage_snapshots,
            elapsed_ms=1250,
        ),
        stage_snapshots=stage_snapshots,
        candidate_count=360,
        candidate_feature_count=352,
        security_master_covered_count=74,
        history_required_sessions=61,
        candidate_feature_coverage_ratio=352 / 360,
        security_master_coverage_ratio=74 / 360,
        candidate_optional_reason_counts=(("missing_listing_date", 221), ("missing_listing_age_sessions", 65)),
        primary_blocker="security_master_coverage_incomplete",
    )

    payload = InputQualityPayload((status,))

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
    assert pipeline["current_stage"] == "action_publish"
    assert pipeline["elapsed_ms"] == 1250
    stages = {stage["key"]: stage for stage in pipeline["stages"]}
    assert tuple(stages) == tuple(stage.value for stage in PipelineStage)
    assert stages["local_data"]["duration_ms"] == 135
    assert stages["fixed_selection"]["not_selected_count"] == 0
    assert payload["tomorrow"]["history_required_sessions"] == 61
    snapshots = payload["tomorrow"]["stage_snapshots"]
    assert [stage["stage"] for stage in snapshots] == [stage.value for stage in PipelineStage]
    assert snapshots[-1]["stage"] == "action_publish"
    assert payload["tomorrow"]["candidate_optional_reason_counts"] == {
        "missing_listing_age_sessions": 65,
        "missing_listing_date": 221,
    }
    assert "supply_funnel" not in payload["tomorrow"]
    assert "asdict(status.pipeline)" not in (
        PROJECT_ROOT / "src/trader/recommendation/infra/status_projection.py"
    ).read_text(encoding="utf-8")


def test_bootstrap_wires_real_outcome_settlement_without_eager_database_write(tmp_path) -> None:
    system = BuildSystem(_config(tmp_path))

    assert type(system.scheduler._dependencies.settlement).__name__ == "OutcomeSettlementAdapter"
    assert not (tmp_path / "runtime" / "research" / "outcomes.sqlite3").exists()
