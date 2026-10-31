"""Resource diagnostics exercise the real admission lifecycle without training."""

import pytest

from scripts.diagnose_runtime import _parser, _validate, build_commands, build_report
from scripts.runtime_diagnostics import training_resources
from scripts.runtime_diagnostics.diagnostic_types import DiagnosticResult


@pytest.mark.parametrize("profiles", (("v5",), ("v1", "v5"), ("v1", "v2", "v3", "v5")))
@pytest.mark.parametrize("cpus", (1, 4))
def test_real_probe_cancels_blocked_wait_and_restores_native_limits(monkeypatch, profiles, cpus):
    monkeypatch.setattr(training_resources, "AvailableTrainingCpus", lambda: cpus)
    report = training_resources.ProbeTrainingResources(profiles)
    assert report["status"] == "passed"
    summary = report["summary"]
    assert summary["available_cpus"] == cpus
    assert summary["fitting_cpu_capacity"] == max(1, cpus - 1)
    assert tuple(item["profile"] for item in summary["profiles"]) == profiles
    assert summary["blocked_wait_cancelled"] and summary["permits_released"]
    assert summary["native_limits_restored"]
    assert summary["models_trained"] == summary["data_writes"] == summary["network_calls"] == 0
    assert summary["full_training_validation"] is False


def test_missing_native_library_evidence_is_failed_not_passed(monkeypatch):
    monkeypatch.setattr(training_resources, "_native_limits", lambda: ())
    report = training_resources.ProbeTrainingResources(("v1",))
    assert report["status"] == "failed"
    result = DiagnosticResult("training_resources", 1, 300, report, None)
    assert build_report("training-resources", (result,))["status"] == "failed"


@pytest.mark.parametrize("profiles", ((), ("v1", "v1"), ("unknown",)))
def test_invalid_selection_is_rejected_before_resource_access(monkeypatch, profiles):
    def forbidden():
        pytest.fail("invalid selection reached CPU discovery")

    monkeypatch.setattr(training_resources, "AvailableTrainingCpus", forbidden)
    with pytest.raises(ValueError, match="training_resource_profiles_invalid"):
        training_resources.ProbeTrainingResources(profiles)


def test_unified_command_preserves_selection_and_caps_child_lifetime():
    options, _ = _validate(_parser().parse_args([
        "--profile", "training-resources", "--training-profiles", "v1,v5",
        "--command-timeout-seconds", "600",
    ]))
    commands = build_commands(options)
    assert len(commands) == 1
    assert commands[0].argv[1:] == (
        "-m", "scripts.runtime_diagnostics.training_resources", "--profiles", "v1,v5",
    )
    assert commands[0].timeout_seconds == 30
