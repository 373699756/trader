from __future__ import annotations

import json
import threading
from pathlib import Path

from trader.bootstrap import build_system

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
