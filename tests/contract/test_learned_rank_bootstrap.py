"""V1 composition contracts without colliding with the general bootstrap test module."""

from __future__ import annotations

import threading
from pathlib import Path

from tests.contract.test_bootstrap import _config
from trader.bootstrap import BuildProfiledSystem, BuildSystem
from trader.entrypoints.cli import BuildParser as CliParser
from trader.entrypoints.performance import Run as PerformanceRun


def test_explicit_v1_build_is_lazy_shadow_and_reuses_v2_published_history(tmp_path: Path, monkeypatch) -> None:
    path = _config(tmp_path)
    monkeypatch.setattr(
        threading.Thread, "start", lambda _thread: (_ for _ in ()).throw(AssertionError("build started a thread"))
    )
    system = BuildSystem(path, scoring_profile="v1")
    status = system.app.test_client().get("/api/status").get_json()
    assert status["scoring_profile"]["profile_id"] == "v1"
    assert all(
        not head["active"] and not head["production_authority"] for head in status["scoring_profile"]["heads"].values()
    )
    assert system.records._database == tmp_path / "data/recommendation/learned_rank/decisions.sqlite3"
    assert system.market_data.history.completed_features_enabled is True
    assert system.market_data.history._history.publication.profile == "v2"
    assert not (tmp_path / "data/recommendation").exists()
    assert not (tmp_path / "runtime").exists()
    # Both retains its two production lanes and their shared physical resource owners.
    both = BuildProfiledSystem(path)
    assert tuple(item.profile_id for item in both.systems) == ("v2", "v3")
    assert both.systems[0].workers.market_cache is both.systems[1].workers.market_cache
    assert both.systems[0].budget is both.systems[1].budget


def test_v1_cli_and_diagnostics_do_not_enter_legacy_training_or_performance(tmp_path: Path) -> None:
    parser = CliParser()
    assert parser.parse_args(["train", "--profile", "v1"]).profile == "v1"
    assert parser.parse_args(["--profile", "v1", "train"]).profile == "v1"
    report = PerformanceRun(_config(tmp_path), scoring_profile="v1")
    assert report["status"] == "failed"
    assert report["failures"] == ["learned_rank_shadow_workload_unverified"]
    assert report["measurements"] == {} and report["network_calls"] == 0
