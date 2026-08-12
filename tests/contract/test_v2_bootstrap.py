from __future__ import annotations

import json
import threading
from pathlib import Path

from trader.bootstrap import _initialize_reference_data_plane, build_system

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
    assert system.long_v2_runtime is not None
    assert system.app.test_client().get("/api/status").status_code == 404
    assert system.app.test_client().get("/api/v2/status").status_code == 200


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
