from __future__ import annotations

import json
import threading
from pathlib import Path
from unittest.mock import Mock

from trader.bootstrap import ApplicationSystem, build_system

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
    assert system.pipeline._market_data.runner.worker_pool is system.pipeline._data_pool
    assert system.pipeline._market_data.research._json_writer._executor is system.pipeline._persistence_pool
    assert system.pipeline._market_data.research.client._json_writer._executor is system.pipeline._persistence_pool
    assert system.pipeline._market_data_manages_workers is True
    assert system.pipeline._data_pool.status()["workers"] == 6
    assert system.pipeline._data_pool.status()["queue_capacity"] == 5
    assert system.pipeline._data_pool.status()["urgent_workers"] == 1
    assert system.pipeline._data_pool.status()["urgent_queue_capacity"] == 1
    assert system.pipeline._data_pool._thread_name_prefix == "source-data"
    assert system.market_cache.status() == {}


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
        state=Mock(),
        market_cache=Mock(),
        history_pool=history_pool,
        source_lanes=Mock(),
    )

    assert system.start() is True
    assert system.start() is False
    history_pool.stop.assert_not_called()
