from dataclasses import asdict, replace
from datetime import date, timedelta
from importlib.util import find_spec

import numpy as np
import pytest

from trader.training.domain.training_dates import BuildTrainingDateSplit
from trader.training.domain.v5.early_stopping import TcnEarlyStopping
from trader.training.infra.profile.v5.configuration import (
    DecodeTcnConfiguration,
    ParseTcnParameters,
    TcnConfigurationDocument,
)
from trader.training.infra.profile.v5.settings import TcnArchitecture, TcnFittingParameters
from trader.training.infra.profile.v5.tensors import DecodeTcnNetwork


def test_patience_ignores_tiny_improvements_but_keeps_best_weights_and_accumulates_gain():
    state = TcnEarlyStopping(0.001)
    assert state.observe(1.0) and state.stale == 0
    assert state.observe(0.9996) and state.stale == 1
    assert state.best == 0.9996 and state.significant_best == 1.0
    assert state.observe(0.9989) and state.stale == 0
    assert not state.observe(0.9989) and state.stale == 1
    assert not state.observe(1.1) and state.stale == 2
    assert state.observe(0.0) and state.stale == 0
    assert not state.observe(0.0) and state.stale == 1
    with pytest.raises(ValueError):
        state.observe(float("nan"))


@pytest.mark.skipif(find_spec("torch") is None, reason="V5 optional CPU dependency")
def test_training_restores_smallest_loss_weights_not_significant_anchor_or_last_epoch(monkeypatch):
    from trader.training.application.tomorrow_training import TrainingFitDetail
    from trader.training.infra.profile.v5.batch_cache import TcnBatchCache
    from trader.training.infra.profile.v5.fitting import _TcnFitState, _TcnProgress
    from trader.training.infra.profile.v5.network import ConfigureTcnRuntime, TcnNetwork

    ConfigureTcnRuntime(training=True)
    parameters = TcnFittingParameters(1, epochs=5, early_stopping_rounds=2, early_stopping_relative_delta=0.001)
    network = TcnNetwork(6, parameters.dropout, parameters.architecture)
    values = np.random.default_rng(47).normal(size=(4, 367))
    cache = TcnBatchCache(values, values, 4)
    losses = (1.0, 0.9996, 1.1)
    weights = []

    def evaluate(state, epoch):
        weights.append(state.network.tensors())
        return losses[epoch - 1]

    monkeypatch.setattr(_TcnFitState, "_evaluate", evaluate)
    state = _TcnFitState(
        network,
        parameters,
        cache,
        lambda: None,
        np.zeros(366),
        np.ones(366),
        _TcnProgress(TrainingFitDetail("fixture", 4, "training"), None),
    )
    assert state.train() == 3
    assert network.tensors() == weights[1]
    assert weights[1] != weights[0] and weights[1] != weights[2]


def test_annual_split_preserves_evaluation_dates_and_supplier_neutral_default_identity():
    dates = tuple(date(2018, 1, 1) + timedelta(days=index) for index in range(2000))
    old = BuildTrainingDateSplit(dates, parent_manifest_hash="a" * 64, fit_dates=750, history_sessions=62)
    new = BuildTrainingDateSplit(
        dates, parent_manifest_hash="a" * 64, fit_dates=250, history_sessions=62, annual_fit=True
    )
    assert new.model_fit_dates == old.model_fit_dates[-250:]
    assert len(new.warmup_dates) == 61 and new.warmup_dates[-1] < new.model_fit_dates[0]
    assert new.early_stopping_dates == old.early_stopping_dates
    assert new.calibration_dates == old.calibration_dates
    assert new.confirmation_dates == old.confirmation_dates
    assert new.daily_proxy_holdout_dates == old.daily_proxy_holdout_dates
    assert new.point_in_time_holdout_dates == old.point_in_time_holdout_dates
    assert old.schema_version == "training_date_split"
    assert new.schema_version == "v5_annual_training_split"
    assert old.content_hash != new.content_hash
    with pytest.raises(ValueError):
        BuildTrainingDateSplit(dates, parent_manifest_hash="a" * 64, fit_dates=250)
    with pytest.raises(ValueError):
        replace(new, calibration_dates=new.model_fit_dates[:20])


@pytest.mark.parametrize(
    "field,value",
    [
        ("hidden_channels", 6),
        ("hidden_channels", True),
        ("compute_dtype", "float16"),
        ("early_stopping_relative_delta", float("nan")),
        ("early_stopping_relative_delta", -1),
        ("compute_threads", 5),
        ("compute_threads", True),
        ("epochs", 0),
        ("early_stopping_rounds", 101),
        ("learning_rate", float("inf")),
    ],
)
def test_invalid_configuration_is_rejected(field, value):
    fields = asdict(TcnFittingParameters(20000))
    fields.pop("minimum_industry_training_rows")
    fields[field] = value
    with pytest.raises(ValueError):
        ParseTcnParameters(fields, 20000)


def test_configuration_snapshot_roundtrip_hash_and_missing_fields():
    parameters = TcnFittingParameters(20000)
    document = TcnConfigurationDocument(250, parameters)
    assert DecodeTcnConfiguration(document) == (250, parameters)
    document["fit_dates"] = 750
    with pytest.raises(ValueError, match="hash"):
        DecodeTcnConfiguration(document)
    fields = asdict(parameters)
    fields.pop("minimum_industry_training_rows")
    fields.pop("compute_dtype")
    with pytest.raises(ValueError):
        ParseTcnParameters(fields, 20000)
    with pytest.raises(ValueError, match="retraining required"):
        DecodeTcnNetwork({"contract": "old", "dropout": 0.1, "weights": []}, 6)


@pytest.mark.parametrize("channels", (4, 8, 16))
@pytest.mark.parametrize("dtype", ("float32", "float64"))
@pytest.mark.parametrize("auxiliary", (5, 6))
@pytest.mark.skipif(find_spec("torch") is None, reason="V5 optional CPU dependency")
def test_parameterized_network_roundtrip_uses_model_precision(channels, dtype, auxiliary):
    import torch

    from trader.training.infra.profile.v5.network import ConfigureTcnRuntime, TcnNetwork

    ConfigureTcnRuntime(training=True)
    architecture = TcnArchitecture(channels, dtype)
    torch.manual_seed(31)
    model = TcnNetwork(auxiliary, 0.1, architecture)
    features = np.random.default_rng(31).normal(size=(3, auxiliary + 360))
    predicted = model.predict(features)
    other = TcnNetwork(auxiliary, 0.1, architecture)
    other.load_tensors(model.tensors())
    np.testing.assert_array_equal(other.predict(features), predicted)
    assert predicted.dtype == np.float64
    assert next(model.parameters()).dtype == (torch.float32 if dtype == "float32" else torch.float64)


@pytest.mark.skipif(find_spec("torch") is None, reason="V5 optional CPU dependency")
def test_runtime_rejects_threads_exceeding_cpu_affinity(monkeypatch):
    from trader.training.infra.profile.v5 import network

    monkeypatch.setattr(network.os, "sched_getaffinity", lambda _pid: {0}, raising=False)
    with pytest.raises(ValueError, match="CPU capacity"):
        network.ConfigureTcnRuntime(training=True, compute_threads=2)


@pytest.mark.skipif(find_spec("torch") is None, reason="V5 optional CPU dependency")
def test_fitting_parameters_invalidate_models_but_only_fit_window_invalidates_samples(tmp_path):
    from tests.unit.training.infra.profile.v5.test_v5_training import ROOT, _archive
    from trader.training.infra.engine import ProfileTrainingRequest, _sample_cache_for, _training_contract_hash
    from trader.training.infra.profile.v5.contracts import V5_TRAINING_PROFILE
    from trader.training.infra.training_settings import LoadProfileTrainingSettings

    profile = V5_TRAINING_PROFILE
    snapshot = _archive(tmp_path / "history").snapshot
    settings = LoadProfileTrainingSettings(ROOT, "v5")

    def identities(options):
        split = BuildTrainingDateSplit(
            snapshot.calendar.open_dates,
            parent_manifest_hash=snapshot.active_snapshot_hash,
            fit_dates=options.fit_dates,
            history_sessions=62,
            annual_fit=True,
        )
        request = ProfileTrainingRequest(
            tmp_path / "history",
            tmp_path / "models",
            profile,
            profile.heads,
            options,
            sample_root=tmp_path / "samples",
        )
        return (
            _sample_cache_for(snapshot, split, request).identity,
            _training_contract_hash(profile, profile.heads[0], options),
        )

    original = identities(settings)
    for field, value in (
        ("hidden_channels", 4),
        ("compute_dtype", "float64" if settings.fitting.compute_dtype == "float32" else "float32"),
        ("early_stopping_relative_delta", 0.01),
        ("compute_threads", 1),
        ("epochs", 29),
    ):
        changed = identities(replace(settings, fitting=replace(settings.fitting, **{field: value})))
        assert changed[0] == original[0]
        assert changed[1] != original[1]
    changed = identities(replace(settings, fit_dates=settings.fit_dates + 1))
    assert changed[0] != original[0] and changed[1] != original[1]
