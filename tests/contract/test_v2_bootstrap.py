from __future__ import annotations

import json
import threading
from pathlib import Path
from unittest.mock import Mock

import pytest

from trader.application.tomorrow_shadow_runtime import ShadowObservingSnapshotIndex
from trader.bootstrap import (
    ApplicationSystem,
    _initialize_reference_data_plane,
    _initialize_tomorrow_evidence,
    build_system,
)
from trader.infra.persistence.tomorrow_shadow_evidence import TomorrowShadowEvidenceUnavailableError

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_build_system_is_lazy_until_start(tmp_path, monkeypatch) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    runtime = json.loads((PROJECT_ROOT / "config" / "v2" / "runtime.json").read_text(encoding="utf-8"))
    runtime["runtime_dir"] = str(tmp_path / "runtime")
    runtime["strategy_config"] = str(PROJECT_ROOT / "config" / "v2" / "strategy.json")
    runtime["long_watchlist"] = str(PROJECT_ROOT / "config" / "v2" / "long_watchlist.json")
    config_path = config_dir / "runtime.json"
    config_path.write_text(json.dumps(runtime), encoding="utf-8")
    started: list[str] = []

    def reject_thread_start(_thread: threading.Thread) -> None:
        started.append("thread")

    monkeypatch.setattr(threading.Thread, "start", reject_thread_start)

    system = build_system(config_path)

    assert system.app is not None
    assert started == []
    assert not (tmp_path / "runtime").exists()
    market_data = system.pipeline._quotes
    assert system.pipeline._market_full is market_data
    assert system.pipeline._candidate_data is market_data
    assert system.pipeline._research is market_data
    assert market_data.runner.worker_pool is system.pipeline._data_pool
    assert market_data.research._json_writer._executor is system.pipeline._persistence_pool
    assert market_data.research.client._json_writer._executor is system.pipeline._persistence_pool
    assert market_data.warmup.status().batch_timeout_seconds == 300.0
    assert system.pipeline._market_data_manages_workers is True
    assert system.pipeline._data_pool.status()["workers"] == 6
    assert system.pipeline._data_pool.status()["queue_capacity"] == 5
    assert system.pipeline._data_pool.status()["urgent_workers"] == 1
    assert system.pipeline._data_pool.status()["urgent_queue_capacity"] == 1
    assert system.pipeline._data_pool._thread_name_prefix == "source-data"
    assert system.market_cache.status() == {}
    assert isinstance(system.pipeline._published_snapshots, ShadowObservingSnapshotIndex)
    assert system.tomorrow_shadow_worker is not None
    assert system.pipeline._tomorrow_native_inputs is system.tomorrow_shadow_worker
    assert system.tomorrow_shadow_worker.status()["running"] is False
    assert system.tomorrow_shadow_worker.status()["native_offered"] == 0
    assert system.tomorrow_shadow_runtime is not None
    research_trace = system.tomorrow_shadow_runtime.research_trace
    assert research_trace is not None
    assert research_trace.status().attempts == 0
    assert research_trace.get("missing") is None
    shadow_status = system.tomorrow_shadow_runtime.status()
    assert shadow_status["processed"] == 0
    assert shadow_status["cutover_gate"]["eligible"] is False
    assert "insufficient_samples" in shadow_status["cutover_gate"]["blockers"]
    status_response = system.app.test_client().get("/api/v2/status")
    assert status_response.status_code == 200
    assert status_response.get_json()["shadow"]["cutover_gate"]["eligible"] is False
    assert not any(key.startswith("research_trace_") for key in status_response.get_json()["shadow"])


def test_shadow_runtime_has_no_history_download_or_external_review_calls() -> None:
    shadow_sources = (
        PROJECT_ROOT / "src" / "trader" / "application" / "tomorrow_shadow_projection.py",
        PROJECT_ROOT / "src" / "trader" / "application" / "tomorrow_shadow_runtime.py",
    )
    source = "\n".join(path.read_text(encoding="utf-8") for path in shadow_sources)

    assert "fetch_history" not in source
    assert "load_history" not in source
    assert ".review(" not in source
    assert "DeepSeekReviewPort" not in source
    assert "MarketDataProvider" not in source


def test_evidence_recovery_failure_blocks_cutover_without_blocking_startup() -> None:
    publication = Mock()
    publication.tomorrow_evidence.initialize.side_effect = TomorrowShadowEvidenceUnavailableError("corrupt")

    _initialize_tomorrow_evidence(publication)

    publication.tomorrow_gate.mark_evidence_failure.assert_called_once_with()


def test_reference_data_plane_recovery_initializes_data_plane_and_loader() -> None:
    market_data = Mock()
    market_data.history = Mock()
    market_data.research = Mock()
    data_plane = Mock()

    _initialize_reference_data_plane(market_data, data_plane)

    data_plane.initialize.assert_called_once_with()
    assert market_data.references.recover.call_count == 1
    assert market_data.history.recover_from_data_plane.call_count == 1
    assert market_data.research.recover_from_data_plane.call_count == 1


def test_reference_data_plane_recovery_fails_openly_without_blocking_startup() -> None:
    market_data = Mock()
    data_plane = Mock()
    market_data.research = Mock()
    market_data.references.recover.side_effect = RuntimeError("recover failed")
    data_plane.initialize.side_effect = RuntimeError("db unavailable")

    _initialize_reference_data_plane(market_data, data_plane)

    data_plane.initialize.assert_called_once_with()
    market_data.references.recover.assert_not_called()
    market_data.research.recover_from_data_plane.assert_not_called()


def test_duplicate_system_start_does_not_stop_running_history_pool() -> None:
    history_pool = Mock()
    history_pool.start.side_effect = (True, False)
    research_pool = Mock()
    research_pool.start.side_effect = (True, False)
    supervisor = Mock()
    supervisor.start.side_effect = (True, False)
    system = ApplicationSystem(
        settings=Mock(),
        strategy=Mock(),
        watchlist=Mock(),
        app=Mock(),
        supervisor=supervisor,
        pipeline=Mock(),
        repository=Mock(),
        publisher=Mock(),
        published_snapshots=Mock(),
        state=Mock(),
        market_cache=Mock(),
        history_pool=history_pool,
        research_pool=research_pool,
        source_lanes=Mock(),
    )

    assert system.start() is True
    assert system.start() is False
    history_pool.stop.assert_not_called()
    research_pool.stop.assert_not_called()


def test_system_start_failure_stops_shadow_worker_with_shutdown_budget() -> None:
    history_pool = Mock()
    history_pool.start.return_value = True
    research_pool = Mock()
    research_pool.start.return_value = True
    supervisor = Mock()
    supervisor.start.side_effect = RuntimeError("initializer failed")
    shadow_worker = Mock()
    shadow_worker.start.return_value = True
    settings = Mock()
    settings.pipeline.shutdown_timeout_seconds = 7.0
    system = ApplicationSystem(
        settings=settings,
        strategy=Mock(),
        watchlist=Mock(),
        app=Mock(),
        supervisor=supervisor,
        pipeline=Mock(),
        repository=Mock(),
        publisher=Mock(),
        published_snapshots=Mock(),
        state=Mock(),
        market_cache=Mock(),
        history_pool=history_pool,
        research_pool=research_pool,
        source_lanes=Mock(),
        tomorrow_shadow_worker=shadow_worker,
    )

    with pytest.raises(RuntimeError, match="initializer failed"):
        system.start()

    history_deadline = history_pool.stop.call_args.kwargs["deadline"]
    assert history_deadline is research_pool.stop.call_args.kwargs["deadline"]
    assert history_deadline is shadow_worker.stop.call_args.kwargs["deadline"]
    assert history_deadline.timeout_seconds == 7.0
    history_pool.stop.assert_called_once_with(wait=True, cancel_futures=True, deadline=history_deadline)
    research_pool.stop.assert_called_once_with(wait=True, cancel_futures=True, deadline=history_deadline)
    shadow_worker.stop.assert_called_once_with(wait=True, deadline=history_deadline)
