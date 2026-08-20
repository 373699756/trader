from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace

from trader.application.decision_observers import DecisionObserverStatus
from trader.bootstrap import _initialize_reference_data_plane, _initialize_research_trace, _runtime_status, build_system

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
    assert system.app.test_client().get("/api/status").status_code == 404
    status = system.app.test_client().get("/api/v2/status")
    assert status.status_code == 200
    assert status.get_json()["phase"] == "closed"


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
        strategy_error_codes=(),
        recent_errors=(),
        deepseek=Mock(),
        control_running=True,
        control_inflight=0,
        control_rejected_count=0,
        refresh_failure_count=0,
        decision_failure_count=0,
        review_failure_count=0,
        local_publish_count=1,
        hybrid_publish_count=0,
        publish_rejection_count=0,
        observer_rejection_count=0,
        freeze_completed_count=0,
        freeze_failure_count=0,
        settlement_completed_count=0,
        settlement_failure_count=0,
        last_error_code="",
    )
    reviewer = Mock()
    reviewer.status.return_value = {"status": "ready"}
    budget = Mock()
    budget.summary.return_value = {"limit": 168, "used": 0, "remaining": 168}

    payload = _runtime_status(scheduler, reviewer, budget)

    assert payload["observer"]["consumer_failure_count"] == 3
    assert payload["health"] == {"level": "degraded", "issue_count": 1}
    assert payload["degraded_reasons"] == ["observer:ResearchTraceCapacityError"]
    assert payload["last_error"] == "observer:ResearchTraceCapacityError"
    assert payload["scheduler"]["settlement_completed_count"] == 0
    assert payload["scheduler"]["settlement_failure_count"] == 0


def test_bootstrap_wires_real_outcome_settlement_without_eager_database_write(tmp_path) -> None:
    system = build_system(_config(tmp_path))

    assert type(system.scheduler._dependencies.settlement).__name__ == "V2OutcomeSettlementAdapter"
    assert not (tmp_path / "runtime" / "research" / "outcomes.sqlite3").exists()
