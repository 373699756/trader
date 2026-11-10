"""Busy evidence must come from pending physical I/O or an unfinished publication."""

from dataclasses import replace
from pathlib import Path
import socket

import pytest

from scripts.recommendation.web.shutdown_runtime import OwnedShutdownOptions, ObserveOwnedRuntimeShutdown, _busy_evidence
from tests.helpers.shared.configuration import RuntimeSettingsFixture
from trader.infra.settings.data_paths import DataPaths


def _status():
    return {
        "runtime_started": True, "phase": "after_close", "selected_profile": "v2",
        "market_data": {"sources": {name: {
            "planned_count": 1, "success_count": 1, "error_count": 0, "physical_fetch_inflight_count": 0,
        } for name in ("tencent_market", "sina", "tencent", "tencent_long")}},
        "scheduler": {"publication_io": []},
    }


def test_acquisition_trigger_requires_a_physical_request_not_an_idle_lane():
    payload = _status()
    assert _busy_evidence(payload, "acquisition") is None
    payload["market_data"]["sources"]["tencent_market"]["planned_count"] = 2
    assert _busy_evidence(payload, "acquisition") is None
    payload["market_data"]["sources"]["tencent_market"]["physical_fetch_inflight_count"] = 1
    assert _busy_evidence(payload, "acquisition").work == ("tencent_market",)
    payload["market_data"]["sources"]["tencent_market"]["physical_fetch_inflight_count"] = 0
    assert _busy_evidence(payload, "acquisition") is None


@pytest.mark.parametrize("pending,latency,matched", [(1, None, True), (0, 2, False), (1, 2, False)])
def test_publication_trigger_does_not_mistake_finished_receipts_for_work(pending, latency, matched):
    payload = _status()
    payload["scheduler"]["publication_io"] = [{
        "operation": "formal_write", "pending_count": pending, "latency_ms": latency,
    }]
    assert (_busy_evidence(payload, "publication") is not None) is matched


def test_malformed_counts_are_rejected_instead_of_signing_busy_evidence():
    payload = _status()
    payload["market_data"]["sources"]["tencent"]["physical_fetch_inflight_count"] = True
    with pytest.raises(ValueError, match="counter_invalid"):
        _busy_evidence(payload, "acquisition")


def test_model_trigger_requires_current_inflight_instead_of_completed_prediction_counts():
    payload = _status()
    payload["scoring_profile"] = {"heads": {name: {
        "prediction_inflight_count": 0, "computation": {"predictor_batch_count": 7},
    } for name in ("tomorrow", "d25")}}
    assert _busy_evidence(payload, "model") is None
    payload["scoring_profile"]["heads"]["d25"]["prediction_inflight_count"] = 1
    assert _busy_evidence(payload, "model").work == ("d25:prediction",)


def test_freeze_trigger_requires_both_heads_to_be_formal_and_ready():
    payload = _status()
    payload["strategies"] = {name: {"status": "ready", "frozen": True} for name in ("tomorrow", "d25")}
    assert _busy_evidence(payload, "frozen") is not None
    payload["strategies"]["d25"]["frozen"] = False
    assert _busy_evidence(payload, "frozen") is None


def test_eight_head_recovery_cannot_be_signed_using_only_one_profile(tmp_path):
    with pytest.raises(ValueError, match="freeze_requires_all_profiles"):
        OwnedShutdownOptions(tmp_path / "runtime.json", tmp_path / "run.log", "frozen", 10, 2, profiles=("v5",))


def test_owned_launch_rejects_an_existing_endpoint_before_creating_a_process(tmp_path, monkeypatch):
    from scripts.recommendation.web import shutdown_runtime

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        original = RuntimeSettingsFixture()
        config_path = tmp_path / "config/runtime.json"
        settings = replace(
            original, server=replace(original.server, port=listener.getsockname()[1]),
            runtime_dir=tmp_path / "runtime", data_paths=DataPaths(tmp_path / "data"),
        )
        monkeypatch.setattr(shutdown_runtime, "LoadRuntimeSettings", lambda path: settings if path == config_path else original)
        monkeypatch.setattr(shutdown_runtime, "ProcessFamily", lambda *a, **k: pytest.fail("unrelated endpoint"))
        with pytest.raises(ValueError, match="endpoint_in_use"):
            ObserveOwnedRuntimeShutdown(OwnedShutdownOptions(
                config_path, tmp_path / "run.log", "acquisition", 10, 2,
            ))
    assert not (tmp_path / "run.log").exists()


def test_owned_launch_rejects_formal_output_paths_before_creating_files(tmp_path, monkeypatch):
    from scripts.recommendation.web import shutdown_runtime

    monkeypatch.setattr(shutdown_runtime, "ProcessFamily", lambda *a, **k: pytest.fail("formal outputs"))
    with pytest.raises(ValueError, match="requires_isolated_outputs"):
        ObserveOwnedRuntimeShutdown(OwnedShutdownOptions(
            Path("config/runtime.json"), tmp_path / "run.log", "acquisition", 10, 2,
        ))
    assert not (tmp_path / "run.log").exists()


@pytest.mark.parametrize("arguments", [
    ["--launch-run"],
    ["--launch-run", "--runtime-config", "config/runtime.json"],
    ["--launch-run", "--send-sigterm"],
    ["--launch-run", "--runtime-config", "config/runtime.json", "--send-sigterm", "--pid", "1"],
    ["--runtime-config", "config/runtime.json", "--pid", "1", "--birth-ticks", "0"],
])
def test_launch_mode_never_infers_permission_or_an_external_target(tmp_path, arguments):
    from scripts.recommendation.web.shutdown import Run

    with pytest.raises(SystemExit) as caught:
        Run("shutdown", ["--shutdown-log", str(tmp_path / "run.log"), *arguments])
    assert caught.value.code == 2
    assert not (tmp_path / "run.log").exists()
