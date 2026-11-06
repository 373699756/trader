"""Frozen product checkouts cannot be used as diagnostic output directories."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from scripts.training import cli
from scripts.training.jobs import training_alignment, training_performance
from scripts.training.options import TrainingOptions
from scripts.training.output_paths import RequireIndependentOutput
from scripts.training.samples import sample_comparison_worker, training_samples
from tests.helpers.shared.configuration import SqlitePolicyFixture


@pytest.mark.parametrize("relation", ("equal", "child", "ancestor"))
def test_output_rejects_all_checkout_overlap_relations(tmp_path, relation):
    checkout = tmp_path / "frozen"
    output = {"equal": checkout, "child": checkout / "new-result", "ancestor": tmp_path}[relation]
    with pytest.raises(ValueError, match="overlaps_product_checkout"):
        RequireIndependentOutput(output, checkout)
    assert not checkout.exists()


def test_output_rejects_symlink_alias_of_frozen_checkout(tmp_path):
    checkout = tmp_path / "frozen"
    checkout.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(checkout, target_is_directory=True)
    with pytest.raises(ValueError, match="overlaps_product_checkout"):
        RequireIndependentOutput(alias / "new-result", checkout)
    assert not (checkout / "new-result").exists()


def test_sibling_output_is_independent_and_does_not_create_files(tmp_path):
    RequireIndependentOutput(tmp_path / "output", tmp_path / "frozen")
    assert not (tmp_path / "output").exists()


@pytest.mark.parametrize(
    ("identity", "selector"),
    (
        ("training-performance", "--baseline-checkout"),
        ("training-samples", "--baseline-checkout"),
        ("training-alignment", "--product-checkout"),
    ),
)
def test_public_training_options_reject_output_inside_the_selected_checkout(tmp_path, identity, selector):
    checkout = tmp_path / "frozen"
    arguments = [
        selector, str(checkout),
        "--training-probe-directory", str(checkout / "new-result"),
    ]
    if identity != "training-samples":
        arguments.extend(("--history-root", str(tmp_path / "history")))
    with pytest.raises(ValueError, match="overlaps_product_checkout"):
        cli.ParseOptions(cli.BuildParser(identity).parse_args(arguments))
    assert not checkout.exists()


def test_benchmark_constructor_rejects_baseline_overlap_without_launching(tmp_path):
    checkout = tmp_path / "frozen"
    (checkout / "src/trader").mkdir(parents=True)
    output = checkout / "new-result"
    options = TrainingOptions(
        "training-performance",
        history_root=tmp_path / "history",
        training_probe_directory=output,
        baseline_checkout=checkout,
    )
    with pytest.raises(ValueError, match="overlaps_product_checkout"):
        training_performance.BuildTrainingBenchmarkCommands(options, "/python3")
    assert not output.exists()


def test_sample_probe_rejects_baseline_overlap_before_workspace_preparation(tmp_path, monkeypatch):
    checkout = tmp_path / "frozen"
    (checkout / "src/trader").mkdir(parents=True)
    config = tmp_path / "config/runtime.json"
    config.parent.mkdir()
    config.write_text("{}", encoding="utf-8")
    config.with_name("performance.json").write_text("{}", encoding="utf-8")
    output = checkout / "new-result"
    monkeypatch.setattr(
        training_samples, "PrepareDatabaseProbeDirectory",
        lambda *_: pytest.fail("workspace preparation must not be reached"),
    )
    with pytest.raises(ValueError, match="overlaps_product_checkout"):
        training_samples.ProbeTrainingSamples(
            output, checkout, config, size=4, sessions=64, sqlite_policy=SqlitePolicyFixture(),
        )
    assert not output.exists()


@pytest.mark.parametrize("construct_features", (False, True))
def test_sample_worker_library_boundaries_reject_output_before_checkout_reads(tmp_path, construct_features):
    checkout, output = tmp_path / "frozen", tmp_path / "frozen/new-result"
    with pytest.raises(ValueError, match="overlaps_product_checkout"):
        if construct_features:
            sample_comparison_worker._construct_samples(
                checkout, output, 4, 2000, medium="ssd", sqlite_policy=SqlitePolicyFixture(),
            )
        else:
            sample_comparison_worker.MeasureSamples(
                checkout, output, 4, 64, medium="ssd", sqlite_policy=SqlitePolicyFixture(),
            )
    assert not checkout.exists()


def test_sample_worker_cli_rejects_output_before_loading_product_configuration(tmp_path, monkeypatch):
    checkout, output = tmp_path / "frozen", tmp_path / "frozen/new-result"
    monkeypatch.setattr(sys, "argv", [
        "samples", "--checkout", str(checkout), "--root", str(output), "--size", "4", "--sessions", "64",
        "--medium", "ssd",
    ])
    with pytest.raises(ValueError, match="overlaps_product_checkout"):
        sample_comparison_worker.Main()
    assert not checkout.exists()


def test_sample_worker_requires_explicit_medium(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", [
        "samples", "--checkout", str(tmp_path / "checkout"), "--root", str(tmp_path / "output"),
        "--size", "1", "--sessions", "1800", "--construct-features",
    ])
    with pytest.raises(SystemExit) as failure:
        sample_comparison_worker.Main()
    assert failure.value.code == 2
    assert "--medium" in capsys.readouterr().err
    assert not (tmp_path / "output").exists()


def test_sample_policy_uses_selected_medium_and_preserves_unknown_configuration(tmp_path):
    checkout = tmp_path / "checkout"
    (checkout / "config").mkdir(parents=True)
    document = json.loads((sample_comparison_worker.TOOL_ROOT / "config/training.json").read_text())
    document["resources"]["ssd"]["samples"]["v1_batch_rows"] = 8192
    (checkout / "config/training.json").write_text(json.dumps(document))
    selected = sample_comparison_worker.SampleArgumentsForCheckout(checkout, medium="ssd")
    other = sample_comparison_worker.SampleArgumentsForCheckout(checkout, medium="hdd")
    assert selected["sample_settings"].v1_batch_rows == 8192
    assert other["sample_settings"].v1_batch_rows == 4096
    unknown = sample_comparison_worker.SampleArgumentsForCheckout(checkout, medium="unknown")
    assert unknown["sample_settings"] == other["sample_settings"]


def test_function_profile_requires_feature_construction_before_configuration_reads(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "argv", [
        "samples", "--checkout", str(tmp_path / "checkout"), "--root", str(tmp_path / "output"),
        "--size", "4", "--sessions", "64", "--medium", "ssd", "--profile-functions",
    ])
    with pytest.raises(ValueError, match="function_profile_requires_feature_construction"):
        sample_comparison_worker.Main()
    assert not (tmp_path / "output").exists()


@pytest.mark.parametrize("existing_profiler", ("thread_hook", "cprofile"))
def test_function_profile_rejects_existing_profiler_without_changing_it(tmp_path, existing_profiler):
    from cProfile import Profile
    from contextlib import ExitStack

    if sys.getprofile() is not None:
        pytest.skip("the test must preserve an external profiling hook")
    if sys.version_info >= (3, 12):
        from sys import monitoring

        if monitoring.get_tool(monitoring.PROFILER_ID) is not None:
            pytest.skip("the test must preserve an external monitoring profiler")

    def caller_profile(*_arguments):
        pass

    with ExitStack() as cleanup:
        if existing_profiler == "thread_hook":
            sys.setprofile(caller_profile)
            cleanup.callback(sys.setprofile, None)
        else:
            cleanup.enter_context(Profile())
        installed_hook = sys.getprofile()
        if sys.version_info >= (3, 12):
            installed_owner = monitoring.get_tool(monitoring.PROFILER_ID)
        checkout, output = tmp_path / "unread-checkout", tmp_path / "output"
        with pytest.raises(ValueError, match="sample_function_profile_already_running"):
            sample_comparison_worker._construct_samples(
                checkout, output, 1, 2000,
                medium="ssd", sqlite_policy=SqlitePolicyFixture(), profile_functions=True,
            )
        assert sys.getprofile() is installed_hook
        if sys.version_info >= (3, 12):
            assert monitoring.get_tool(monitoring.PROFILER_ID) == installed_owner
        assert not checkout.exists() and not output.exists()
    assert sys.getprofile() is None
    if sys.version_info >= (3, 12):
        assert monitoring.get_tool(monitoring.PROFILER_ID) is None


@pytest.mark.parametrize("failure", (RuntimeError("builder_failed"), InterruptedError("builder_cancelled")))
def test_function_profile_releases_its_state_when_sample_builder_fails(tmp_path, monkeypatch, failure):
    from trader.training.infra import sample_builder

    if sys.getprofile() is not None:
        pytest.skip("the test must preserve an external profiling hook")
    if sys.version_info >= (3, 12):
        from sys import monitoring

        if monitoring.get_tool(monitoring.PROFILER_ID) is not None:
            pytest.skip("the test must preserve an external monitoring profiler")

    def fail_builder(_request):
        raise failure

    monkeypatch.setattr(sample_builder, "BuildTrainingSamples", fail_builder)
    with pytest.raises(type(failure)) as observed:
        sample_comparison_worker._construct_samples(
            sample_comparison_worker.TOOL_ROOT, tmp_path / "output", 1, 2000,
            medium="ssd", sqlite_policy=SqlitePolicyFixture(), profile_functions=True,
        )
    assert observed.value is failure
    assert sys.getprofile() is None
    if sys.version_info >= (3, 12):
        assert monitoring.get_tool(monitoring.PROFILER_ID) is None


def test_function_profile_is_bounded_and_omits_external_paths(tmp_path):
    import platform
    from cProfile import Profile
    from types import FunctionType

    def template():
        return sum(range(32))

    checkout = tmp_path / "checkout"
    profiler = Profile()
    for number in range(40):
        code = template.__code__.replace(
            co_filename=str(checkout / "src" / f"fixture_{number}.py"), co_name=f"fixture_{number}",
        )
        profiler.runcall(FunctionType(code, globals()))
    private = template.__code__.replace(co_filename="/private/person/credentials.py", co_name="external")
    profiler.runcall(FunctionType(private, globals()))
    tool = template.__code__.replace(
        co_filename=str(sample_comparison_worker.TOOL_ROOT / "scripts/tools.py"), co_name="tool_fixture",
    )
    profiler.runcall(FunctionType(tool, globals()))
    report = sample_comparison_worker._project_function_profile(profiler, checkout)
    encoded = json.dumps(report)
    assert str(tmp_path) not in encoded and "/private" not in encoded and "credentials" not in encoded
    assert str(sample_comparison_worker.TOOL_ROOT) not in encoded
    assert report["limit"] == 30 and len(report["top_functions"]) == 30
    assert report["function_count"] > 30
    assert report["fixture"] and report["instrumented"] and not report["performance_evidence"]
    assert not report["production_authority"] and report["cumulative_times_overlap"]
    assert report["timer"] == "process_cpu_seconds; includes_CPU_of_all_process_threads"
    assert report["event_scope"] == "interpreter_profile_events; thread_coverage_depends_on_python_runtime"
    assert report["python_implementation"] == platform.python_implementation()
    assert report["python_version"] == platform.python_version()
    assert report["thread_isolation"] is False
    assert report["scope"] == (
        "enabled_during_sample_builders; sequential_fixture; cProfile_overhead_included; no_model_fitting"
    )
    assert all(
        row["calls"] >= row["primitive_calls"] > 0
        and row["self_cpu_seconds"] >= 0 and row["cumulative_cpu_seconds"] >= 0
        and (row["path"] is None or not Path(row["path"]).is_absolute())
        for row in report["top_functions"]
    )


def test_real_sample_profiler_preserves_all_four_numeric_results_and_is_opt_in(tmp_path, monkeypatch):
    import cProfile

    arguments = (sample_comparison_worker.TOOL_ROOT, 1, 2000)
    with monkeypatch.context() as scope:
        scope.setattr(cProfile, "Profile", lambda **_: pytest.fail("default mode must not enable cProfile"))
        baseline = sample_comparison_worker._construct_samples(
            arguments[0], tmp_path / "baseline", arguments[1], arguments[2],
            medium="ssd", sqlite_policy=SqlitePolicyFixture(),
        )
    measured = sample_comparison_worker._construct_samples(
        arguments[0], tmp_path / "profiled", arguments[1], arguments[2],
        medium="ssd", sqlite_policy=SqlitePolicyFixture(), profile_functions=True,
    )
    assert baseline["sample_policy_medium"] == measured["sample_policy_medium"] == "ssd"
    assert "function_profile" not in baseline
    assert set(baseline["cases"]) == set(measured["cases"]) == {"v1", "v2", "v3", "v5"}
    assert all(baseline["cases"][key]["numeric_hash"] == measured["cases"][key]["numeric_hash"] for key in baseline["cases"])
    profile = measured["function_profile"]
    assert profile["enabled"] and 0 < len(profile["top_functions"]) <= 30
    assert measured["models_trained"] == 0 and not measured["shared_training_job"]
    assert any(row["source"] == "checkout" for row in profile["top_functions"])
    assert all(
        row["function"] not in {"LoadProfileTrainingSettings", "_project_function_profile"}
        for row in profile["top_functions"]
    )


def test_alignment_worker_rejects_selected_product_overlap_before_fitting(tmp_path, monkeypatch, capsys):
    checkout, output = tmp_path / "frozen", tmp_path / "frozen/new-result"
    monkeypatch.setattr(training_alignment, "_select_product", lambda _: checkout)
    monkeypatch.setattr(training_alignment, "_probe", lambda *_args, **_kwargs: pytest.fail("fit must not be reached"))
    monkeypatch.setattr(sys, "argv", [
        "alignment", "--product-checkout", str(checkout), "--history-root", str(tmp_path / "history"),
        "--work-root", str(output),
    ])
    assert training_alignment.Main() == 1
    assert json.loads(capsys.readouterr().out)["status"] == "failed"
    assert not checkout.exists()
