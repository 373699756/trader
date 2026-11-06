"""Frozen product checkouts cannot be used as diagnostic output directories."""

from __future__ import annotations

import json
import sys

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
                checkout, output, 4, 2000, sqlite_policy=SqlitePolicyFixture(),
            )
        else:
            sample_comparison_worker.MeasureSamples(
                checkout, output, 4, 64, sqlite_policy=SqlitePolicyFixture(),
            )
    assert not checkout.exists()


def test_sample_worker_cli_rejects_output_before_loading_product_configuration(tmp_path, monkeypatch):
    checkout, output = tmp_path / "frozen", tmp_path / "frozen/new-result"
    monkeypatch.setattr(sys, "argv", [
        "samples", "--checkout", str(checkout), "--root", str(output), "--size", "4", "--sessions", "64",
    ])
    with pytest.raises(ValueError, match="overlaps_product_checkout"):
        sample_comparison_worker.Main()
    assert not checkout.exists()


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
