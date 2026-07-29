from __future__ import annotations

import json
import threading
from pathlib import Path
from unittest.mock import Mock

import pytest

from trader.application.tomorrow_shadow_runtime import ShadowObservingSnapshotIndex
from trader.bootstrap import ApplicationSystem, _initialize_tomorrow_evidence, build_system
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
    shadow_status = system.tomorrow_shadow_runtime.status()
    assert shadow_status["processed"] == 0
    assert shadow_status["cutover_gate"]["eligible"] is False
    assert "insufficient_samples" in shadow_status["cutover_gate"]["blockers"]
    status_response = system.app.test_client().get("/api/v2/status")
    assert status_response.status_code == 200
    assert status_response.get_json()["shadow"]["cutover_gate"]["eligible"] is False


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


def test_duplicate_system_start_does_not_stop_running_history_pool() -> None:
    history_pool = Mock()
    history_pool.start.side_effect = (True, False)
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
        source_lanes=Mock(),
    )

    assert system.start() is True
    assert system.start() is False
    history_pool.stop.assert_not_called()


def test_system_start_failure_stops_shadow_worker_with_shutdown_budget() -> None:
    history_pool = Mock()
    history_pool.start.return_value = True
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
        source_lanes=Mock(),
        tomorrow_shadow_worker=shadow_worker,
    )

    with pytest.raises(RuntimeError, match="initializer failed"):
        system.start()

    history_pool.stop.assert_called_once_with(wait=True, cancel_futures=True)
    shadow_worker.stop.assert_called_once_with(wait=True, timeout_seconds=7.0)
