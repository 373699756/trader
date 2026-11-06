"""Exact network inputs and bounded lifetime for opt-in normalized TCN batches."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date, timedelta
from importlib.util import find_spec
from pathlib import Path
from weakref import ref

import numpy as np
import pytest

from tests.helpers.shared.configuration import SqlitePolicyFixture
from tests.helpers.training.configuration import TcnFittingParametersFixture
from tests.helpers.training.training_resources import TrainingSampleSettingsFixture
from trader.training.domain.training_dates import BuildTrainingDateSplit
from trader.training.infra.execution_options import TrainingExecutionOptions
from trader.training.infra.profile.v5 import batch_cache
from trader.training.infra.profile.v5.contracts import V5_TRAINING_PROFILE
from trader.training.infra.sample_index import SQLiteTrainingSampleIndex, TrainingSample

pytestmark = pytest.mark.skipif(find_spec("torch") is None, reason="V5 requires the optional CPU tensor dependency")


@contextmanager
def _samples(root: Path, limit: int):
    contract = V5_TRAINING_PROFILE.heads[0]
    split = BuildTrainingDateSplit(
        tuple(date(2018, 1, 1) + timedelta(days=index) for index in range(2000)),
        parent_manifest_hash="a" * 64,
        fit_dates=61,
        history_sessions=61,
        recent_fit=True,
        history_excludes_signal=True,
    )
    generator = np.random.default_rng(357)
    with SQLiteTrainingSampleIndex(
        root / "samples.sqlite3",
        contract.feature_manifest.names,
        sqlite_policy=SqlitePolicyFixture(),
        sample_settings=TrainingSampleSettingsFixture(tcn_batch_memory_bytes=limit),
    ) as samples:
        for days in (split.model_fit_dates[-3:], split.early_stopping_dates[:2], split.calibration_dates[:3]):
            for day in days:
                for number in range(5):
                    features = generator.normal(size=len(contract.feature_manifest.names))
                    features[number] = -0.0
                    samples.add_final((TrainingSample(
                        f"60000{number}", day, "main", "fixture", 1e8, tuple(features),
                        tuple(float(value) for value in generator.normal(scale=0.01, size=6)),
                    ),))
        samples.prepare_for_model_fitting(split)
        yield samples


@pytest.mark.slow_training
@pytest.mark.parametrize("dtype", ("float32", "float64"))
@pytest.mark.parametrize("head", (0, 1))
@pytest.mark.parametrize("limit", (1, 256 * 1024**2))
def test_prepared_tensors_preserve_real_ten_epoch_fit_and_rng(tmp_path, monkeypatch, dtype, head, limit):
    import torch
    from trader.training.infra.profile.v5 import fitting
    from trader.training.infra.profile.v5.network import ConfigureTcnRuntime, PreserveTcnRuntime

    contract = V5_TRAINING_PROFILE.heads[head]
    parameters = TcnFittingParametersFixture(
        minimum_industry_training_rows=1, batch_size=4, epochs=10, compute_dtype=dtype, compute_threads=1,
    )
    buffers = []
    raw_arrays = []
    original_cache = batch_cache.CacheTcnBatches

    @contextmanager
    def tracked(*args, **kwargs):
        with original_cache(*args, **kwargs) as cache:
            raw_arrays.extend((ref(cache.training), ref(cache.early_stopping)))
            if limit == 1:
                buffers.extend((cache.training.base, cache.early_stopping.base))
            yield cache

    monkeypatch.setattr(fitting, "CacheTcnBatches", tracked)
    with PreserveTcnRuntime(), _samples(tmp_path, limit) as samples:
        ConfigureTcnRuntime(training=True, compute_threads=1)
        counts = samples.industry_counts(contract)[0]
        source = (tmp_path / "samples.sqlite3").read_bytes()
        baseline_progress = []
        baseline = fitting.FitTcnIndustry(
            samples, counts, contract, parameters, lambda: None, report=baseline_progress.append,
        )
        baseline_rng = torch.rand(7)
        buffers.clear()
        raw_arrays.clear()
        samples.execution_options = TrainingExecutionOptions(normalized_tcn_batches=True)
        original_forward = fitting.TcnNetwork.forward

        def checked_forward(network, values):
            assert all(buffer.closed for buffer in buffers)
            assert all(reference() is None for reference in raw_arrays)
            return original_forward(network, values)

        monkeypatch.setattr(fitting.TcnNetwork, "forward", checked_forward)
        prepared_progress = []
        prepared = fitting.FitTcnIndustry(
            samples, counts, contract, parameters, lambda: None, report=prepared_progress.append,
        )
        assert prepared == baseline
        assert prepared_progress == baseline_progress
        assert torch.equal(torch.rand(7), baseline_rng)
        assert (tmp_path / "samples.sqlite3").read_bytes() == source
        assert not tuple(tmp_path.glob("tcn-batches-*"))


@pytest.mark.parametrize("dtype", ("float32", "float64"))
def test_prepared_input_bits_and_stopping_labels_keep_original_precision(tmp_path, dtype):
    import torch

    width = 366
    values = np.random.default_rng(71).normal(size=(5, width + 1))
    values[0, :5] = (-0.0, 1e-46, -1e-46, 1.00000007, -1.00000007)
    values[:, -1] = (0.123456789123, -0.0, 1e-46, -1e-46, 0.00123456789123)
    cache = batch_cache.TcnBatchCache(values, values.copy(), 2)
    means, scales = np.zeros(width), np.ones(width)
    target_dtype = torch.float32 if dtype == "float32" else torch.float64
    with batch_cache.PrepareTcnBatches(
        cache, means, scales, workspace=tmp_path, memory_bytes=10**8,
        compute_dtype=dtype, heartbeat=lambda: None,
    ) as prepared:
        expected = torch.from_numpy((values[:, :-1] - means) / scales).to(target_dtype)
        assert torch.equal(prepared.training.view(torch.int32 if dtype == "float32" else torch.int64),
                           expected.view(torch.int32 if dtype == "float32" else torch.int64))
        assert torch.equal(prepared.training_labels, torch.from_numpy(values[:, -1].copy()).to(target_dtype))
        assert prepared.early_stopping_labels.dtype == np.float64
        np.testing.assert_array_equal(prepared.early_stopping_labels.view(np.uint64), values[:, -1].view(np.uint64))
        assert not prepared.early_stopping_labels.flags.writeable


def test_transition_budget_maps_prepared_arrays_and_cancellation_closes_all_buffers(tmp_path, monkeypatch):
    contract = V5_TRAINING_PROFILE.heads[0]
    buffers = []
    original_mmap = batch_cache.mmap.mmap

    def tracked_mmap(*args, **kwargs):
        buffer = original_mmap(*args, **kwargs)
        buffers.append(buffer)
        return buffer

    monkeypatch.setattr(batch_cache.mmap, "mmap", tracked_mmap)
    with _samples(tmp_path, 256 * 1024**2) as samples:
        counts = samples.industry_counts(contract)[0]
        with batch_cache.CacheTcnBatches(samples, counts, contract, 2, lambda: None) as raw:
            limit = raw.training.nbytes + raw.early_stopping.nbytes
            assert not tuple(tmp_path.glob("tcn-batches-*"))
            with batch_cache.PrepareTcnBatches(
                raw, np.zeros(raw.training.shape[1] - 1), np.ones(raw.training.shape[1] - 1),
                workspace=tmp_path, memory_bytes=limit, compute_dtype="float32", heartbeat=lambda: None,
            ):
                assert len(buffers) == 2
                assert tuple(tmp_path.glob("tcn-batches-*"))
            assert all(buffer.closed for buffer in buffers)
            calls = 0

            def cancelled():
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise InterruptedError("fixture cancelled conversion")

            with pytest.raises(InterruptedError, match="cancelled conversion"), batch_cache.PrepareTcnBatches(
                raw, np.zeros(raw.training.shape[1] - 1), np.ones(raw.training.shape[1] - 1),
                workspace=tmp_path, memory_bytes=limit, compute_dtype="float32", heartbeat=cancelled,
            ):
                pytest.fail("cancelled conversion completed")
        assert all(buffer.closed for buffer in buffers)
        assert not tuple(tmp_path.glob("tcn-batches-*"))


@pytest.mark.slow_training
def test_cancelled_prepared_real_fit_closes_inputs_and_preserves_samples(tmp_path, monkeypatch):
    from trader.training.infra.profile.v5 import fitting
    from trader.training.infra.profile.v5.network import ConfigureTcnRuntime, PreserveTcnRuntime

    contract = V5_TRAINING_PROFILE.heads[0]
    parameters = TcnFittingParametersFixture(minimum_industry_training_rows=1, batch_size=4, compute_threads=1)
    cancelled = False
    buffers = []
    original_mmap = batch_cache.mmap.mmap

    def tracked_mmap(*args, **kwargs):
        buffer = original_mmap(*args, **kwargs)
        buffers.append(buffer)
        return buffer

    monkeypatch.setattr(batch_cache.mmap, "mmap", tracked_mmap)

    def checkpoint():
        if cancelled:
            raise InterruptedError("fixture cancelled fitting")

    def progress(detail):
        nonlocal cancelled
        if detail.phase == "training" and detail.completed_batches == 1:
            cancelled = True

    with PreserveTcnRuntime(), _samples(tmp_path, 1) as samples:
        ConfigureTcnRuntime(training=True, compute_threads=1)
        samples.execution_options = TrainingExecutionOptions(normalized_tcn_batches=True)
        source = (tmp_path / "samples.sqlite3").read_bytes()
        with pytest.raises(InterruptedError, match="cancelled fitting"):
            fitting.FitTcnIndustry(
                samples, samples.industry_counts(contract)[0], contract, parameters, checkpoint, report=progress,
            )
        assert (tmp_path / "samples.sqlite3").read_bytes() == source
    assert len(buffers) == 4 and all(buffer.closed for buffer in buffers)
    assert not tuple(tmp_path.glob("tcn-batches-*"))
