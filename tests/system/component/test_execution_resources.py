"""Execution limits reach composed resources without starting or publishing them."""

from __future__ import annotations

import json
import shutil
import threading
from pathlib import Path

import pytest

from trader.bootstrap import BuildProfiledSystem
from trader.infra.settings import ConfigurationError, LoadRuntimeSettings
from trader.recommendation.infra.settings.loading import LoadStrategySettings

ROOT = Path(__file__).resolve().parents[3]


def _config(tmp_path: Path, changes=()) -> Path:
    directory = tmp_path / "config"
    shutil.copytree(ROOT / "config", directory)
    path = directory / "scheduler.json"
    document = json.loads(path.read_text())
    for fields, value in changes:
        target = document
        for field in fields[:-1]:
            target = target[field]
        target[fields[-1]] = value
    path.write_text(json.dumps(document))
    return directory / "runtime.json"


def test_default_executor_configuration_preserves_all_existing_capacities(tmp_path) -> None:
    before = set(threading.enumerate())
    system = BuildProfiledSystem(_config(tmp_path), profiles=("v2", "v3"))
    owner, other = system.systems
    expected = (
        (owner.workers.control_pool, 2, 1, 4),
        (other.workers.control_pool, 2, 1, 4),
        (owner.data_pool, 6, 1, 5),
        (owner.quote_pool, 4, 0, 4),
        (owner.market_quote_pool, 3, 0, 3),
        (owner.minute_pool, 5, 0, 5),
    )
    for pool, workers, urgent, capacity in expected:
        status = pool.status()
        assert (status.workers, status.urgent_workers, status.queue_capacity) == (workers, urgent, capacity)
        assert not status.running and status.inflight == 0
    assert owner.market_data.intraday._workers == 5
    assert owner.workers.http_executor._capacity == 12
    assert set(threading.enumerate()) == before
    assert not (tmp_path / "data").exists()
    assert not (tmp_path / ".runtime").exists()


def test_changed_capacities_reach_all_consumers_and_preserve_shared_ownership(tmp_path) -> None:
    changes = (
        (("pipeline", "executors", "control", "normal_workers"), 2),
        (("pipeline", "executors", "control", "urgent_workers"), 2),
        (("pipeline", "executors", "control", "queue_capacity"), 0),
        (("pipeline", "executors", "source_data", "normal_workers"), 2),
        (("pipeline", "executors", "source_data", "urgent_workers"), 0),
        (("pipeline", "executors", "source_data", "queue_capacity"), 0),
        (("pipeline", "executors", "candidate_quotes", "workers"), 2),
        (("pipeline", "executors", "candidate_quotes", "queue_capacity"), 0),
        (("pipeline", "executors", "market_quotes", "workers"), 1),
        (("pipeline", "executors", "market_quotes", "queue_capacity"), 0),
        (("pipeline", "executors", "intraday_minutes", "workers"), 7),
        (("pipeline", "executors", "intraday_minutes", "queue_capacity"), 0),
        (("pipeline", "executors", "http_workers"), 3),
    )
    config = _config(tmp_path, changes)
    baseline = LoadRuntimeSettings(ROOT / "config/runtime.json")
    system = BuildProfiledSystem(config, profiles=("v1", "v2", "v3", "v5"))
    owner, *others = system.systems
    expected = (
        (owner.workers.control_pool, 4, 2),
        (owner.data_pool, 2, 0),
        (owner.quote_pool, 2, 0),
        (owner.market_quote_pool, 1, 0),
        (owner.minute_pool, 7, 0),
    )
    for pool, workers, urgent in expected:
        status = pool.status()
        assert (status.workers, status.urgent_workers, status.queue_capacity) == (workers, urgent, 0)
    assert owner.market_data.intraday._workers == 7
    assert owner.workers.http_executor._capacity == 3
    for other in others:
        assert other.workers.control_pool is not owner.workers.control_pool
        assert other.workers.control_pool.status().workers == 4
        assert other.minute_pool is owner.minute_pool
        assert other.market_data.intraday is owner.market_data.intraday
        assert other.workers.http_executor is owner.workers.http_executor
        assert not other.owns_shared_resources
    assert system.settings.config_version != baseline.config_version
    assert LoadStrategySettings(config.parent / "strategy.json").strategy_version == LoadStrategySettings(
        ROOT / "config/strategy.json"
    ).strategy_version


@pytest.mark.parametrize("pool", ("control", "source_data", "candidate_quotes", "market_quotes", "intraday_minutes"))
@pytest.mark.parametrize("value", (-1, True, 1.5, None))
def test_executor_queues_reject_invalid_types_and_negative_capacity(tmp_path, pool, value) -> None:
    config = _config(tmp_path, ((("pipeline", "executors", pool, "queue_capacity"), value),))
    with pytest.raises(ConfigurationError):
        LoadRuntimeSettings(config)


@pytest.mark.parametrize(
    "fields",
    (
        ("control", "normal_workers"), ("control", "urgent_workers"),
        ("source_data", "normal_workers"), ("candidate_quotes", "workers"),
        ("market_quotes", "workers"), ("intraday_minutes", "workers"), ("http_workers",),
    ),
)
@pytest.mark.parametrize("value", (0, True, 1.5))
def test_executor_workers_require_positive_integers(tmp_path, fields, value) -> None:
    config = _config(tmp_path, ((("pipeline", "executors", *fields), value),))
    with pytest.raises(ConfigurationError):
        LoadRuntimeSettings(config)


@pytest.mark.parametrize("name", ("market_workers", "normalization_workers", "strategy_workers", "deepseek_workers"))
def test_removed_worker_fields_are_rejected_instead_of_silently_ignored(tmp_path, name) -> None:
    config = _config(tmp_path, ((("pipeline", name), 2),))
    with pytest.raises(ConfigurationError, match="unknown keys"):
        LoadRuntimeSettings(config)


@pytest.mark.parametrize("changes", (
    ((("pipeline", "executors"), {}),),
    ((("pipeline", "executors", "candidate_quotes"), {"workers": 1}),),
    ((("pipeline", "executors", "intraday_minutes", "extra"), 1),),
    ((("pipeline", "executors", "source_data", "urgent_workers"), -1),),
))
def test_executor_structure_is_complete_and_exact(tmp_path, changes) -> None:
    with pytest.raises(ConfigurationError):
        LoadRuntimeSettings(_config(tmp_path, changes))
