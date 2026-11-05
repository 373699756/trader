"""Independent real fit/reload and cancellation without relative quality claims."""

from tests.helpers.training_resources import TrainingSampleSettingsFixture

import hashlib
import json
import time
from contextlib import contextmanager
from datetime import date, timedelta

import numpy as np
import pytest
import torch

from scripts.training.models import v5_lite
from tests.helpers.configuration import SqlitePolicyFixture, TcnFittingParametersFixture
from trader.training.domain.training_dates import BuildTrainingDateSplit
from trader.training.infra.profile.v5.contracts import V5_TRAINING_PROFILE
from trader.training.infra.sample_cache import TrainingSampleCache
from trader.training.infra.sample_index import TrainingSample
from trader.training.infra.sample_sequences import SampleSequenceLayout


def _source(root, fit_dates=61):
    contract = V5_TRAINING_PROFILE.heads[0]
    split = BuildTrainingDateSplit(
        tuple(date(2018, 1, 1) + timedelta(days=i) for i in range(2000)),
        parent_manifest_hash="a" * 64,
        fit_dates=fit_dates,
        recent_fit=True,
        history_sessions=61,
        history_excludes_signal=True,
    )
    cache = TrainingSampleCache(
        root, "independent-hdd-fixture", contract.feature_manifest.names, split.content_hash,
        sequence_layout=SampleSequenceLayout(6, 60, 6), sqlite_policy=SqlitePolicyFixture(),
        sample_settings=TrainingSampleSettingsFixture(),
    )
    rng = np.random.default_rng(344)
    with cache.open() as samples:
        for industry, stocks in (("small", 2), ("large", 3)):
            for days in (split.model_fit_dates[-3:], split.early_stopping_dates[:3], split.calibration_dates[:3], split.confirmation_dates[:3]):
                samples.add_final(
                    TrainingSample(
                        f"{stocks}{stock:05d}", day, "main", industry, 1e8,
                        tuple(rng.normal(size=len(contract.feature_manifest.names))),
                        tuple(float(rng.normal(scale=0.01)) for _ in range(6)),
                    )
                    for day in days for stock in range(stocks)
                )
        samples.prepare_for_model_fitting(split)
        cache.seal(samples)
    return {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in root.iterdir() if path.is_file()}


@pytest.mark.parametrize("cancel", (None, 1))
@pytest.mark.parametrize("fit_dates", (61, 250))
def test_largest_real_dual_head_fit_or_cancel_closes_mappings_and_preserves_source(tmp_path, monkeypatch, cancel, fit_dates):
    from trader.training.infra.profile.v5 import batch_cache, fitting

    source = tmp_path / "source"
    before = _source(source, fit_dates)
    work = tmp_path / "work"
    work.mkdir()
    buffers = []
    original = batch_cache.CacheTcnBatches

    @contextmanager
    def tracked(*args, **kwargs):
        with original(*args, **kwargs) as cache:
            buffers.extend((cache.training.base, cache.early_stopping.base))
            yield cache

    monkeypatch.setattr(fitting, "CacheTcnBatches", tracked)
    threads = torch.get_num_threads()
    deterministic = torch.are_deterministic_algorithms_enabled()
    result = v5_lite._largest(
        source, work, time.monotonic() + 60,
        (61, TcnFittingParametersFixture(minimum_industry_training_rows=1, batch_size=4, compute_threads=1)),
        sqlite_policy=SqlitePolicyFixture(), cancel_after_batches=cancel,
        sample_settings=TrainingSampleSettingsFixture(tcn_batch_memory_bytes=1),
    )
    assert result["passed"] and result["source_unchanged"]
    assert result["quality_evidence"] is False and result["performance_evidence"] is False
    assert result["models_published"] == 0
    assert result["coverage"]["industry"] == "large"
    assert result["coverage"]["eligible_industries"] == 2
    assert {head["head"] for head in result["heads"]} == {"tomorrow", "d25"}
    assert all(head["batch_workspaces_released"] for head in result["heads"])
    assert len(buffers) == 4 and all(buffer.closed for buffer in buffers)
    assert not tuple(work.glob("tcn-batches-*"))
    assert threads == torch.get_num_threads() and deterministic == torch.are_deterministic_algorithms_enabled()
    assert {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in source.iterdir() if path.is_file()} == before
    if cancel is not None:
        assert all(head["cancellation_observed"] and not head["fit_completed"] for head in result["heads"])
        assert not tuple(work.glob("*-model.json"))
    else:
        assert len(tuple(work.glob("*-model.json"))) == 2
        assert all(head["reload_predictions_exact"] and head["model_snapshot_exact"] for head in result["heads"])
        assert all(head["predicted_rows"] == head["counts"]["validation"] == 9 for head in result["heads"])


def test_largest_entry_failure_restores_runtime_and_cleans_snapshots(tmp_path, monkeypatch, capsys):
    source = tmp_path / "source"
    before = _source(source)
    work = tmp_path / "work"
    parameters = TcnFittingParametersFixture(minimum_industry_training_rows=1, compute_threads=1)
    monkeypatch.setattr(v5_lite, "_parameters", lambda *_args, **_kwargs: (61, parameters))
    monkeypatch.setattr(v5_lite, "ProbeLiteKernels", lambda: pytest.fail("lifecycle ran performance kernels"))
    monkeypatch.setattr(v5_lite, "_quality", lambda *_args, **_kwargs: pytest.fail("lifecycle ran relative quality"))
    original = v5_lite.AtomicWriteJson

    def corrupt(path, value, **kwargs):
        if path.name.endswith("-model.json"):
            value["industries"]["large"]["transformer_means"][0] += 1
        original(path, value, **kwargs)

    monkeypatch.setattr(v5_lite, "AtomicWriteJson", corrupt)
    monkeypatch.setattr("sys.argv", [
        "v5_lite", "--work-root", str(work), "--sample-root", str(source),
        "--candidate-config", "fixture", "--largest-only",
    ])
    threads = torch.get_num_threads()
    assert v5_lite.Main() == 1
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "degraded"
    assert "normalization or calibration changed" in report["summary"]["failure"]
    assert report["summary"]["quality_evidence"] is False
    assert threads == torch.get_num_threads()
    assert not tuple(work.glob("*.sqlite3")) and not tuple(work.glob("tcn-batches-*"))
    assert {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in source.iterdir() if path.is_file()} == before


@pytest.mark.parametrize("cancel", (None, 1))
def test_largest_entry_skips_comparison_and_compute_and_cleans_snapshots(tmp_path, monkeypatch, capsys, cancel):
    source = tmp_path / "source"
    _source(source)
    work = tmp_path / "work"
    parameters = TcnFittingParametersFixture(minimum_industry_training_rows=1, compute_threads=1)
    monkeypatch.setattr(v5_lite, "_parameters", lambda *_args, **_kwargs: (61, parameters))
    monkeypatch.setattr(v5_lite, "ProbeLiteKernels", lambda: pytest.fail("lifecycle ran performance kernels"))
    monkeypatch.setattr(v5_lite, "_quality", lambda *_args, **_kwargs: pytest.fail("lifecycle ran relative quality"))
    monkeypatch.setattr("sys.argv", [
        "v5_lite", "--work-root", str(work), "--sample-root", str(source),
        "--candidate-config", "fixture", "--largest-only",
        *([] if cancel is None else ["--cancel-after-batches", str(cancel)]),
    ])
    assert v5_lite.Main() == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "passed" and report["summary"]["compute_probe_skipped"]
    assert report["summary"]["quality_evidence"] is False
    assert "quality" not in report["summary"] and "reference_configuration" not in report["summary"]
    assert report["summary"]["lifecycle"]["cancellation_only"] == (cancel is not None)
    assert not tuple(work.glob("*.sqlite3"))


@pytest.mark.parametrize("arguments", (
    ["--largest-only", "--quality-only"],
    ["--largest-only", "--reference-config", "old.json"],
    ["--largest-only", "--prepare-only"],
    ["--cancel-after-batches", "1"],
    ["--largest-only", "--cancel-after-batches", "0"],
))
def test_largest_rejects_mixed_quality_or_invalid_cancellation_before_writing(tmp_path, monkeypatch, arguments):
    work = tmp_path / "work"
    monkeypatch.setattr("sys.argv", ["v5_lite", "--work-root", str(work), *arguments])
    with pytest.raises(ValueError):
        v5_lite.Main()
    assert not work.exists()
