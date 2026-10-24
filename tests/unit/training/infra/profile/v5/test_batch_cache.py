"""Float64 batch identity and lifetime for memory and file-backed TCN caches."""

from datetime import date, timedelta

import numpy as np
import pytest

from trader.training.domain.training_dates import BuildTrainingDateSplit
from trader.training.infra.profile.v5 import batch_cache
from trader.training.infra.profile.v5.contracts import V5_TRAINING_PROFILE
from trader.training.infra.sample_index import SQLiteTrainingSampleIndex, TrainingSample


@pytest.mark.parametrize("limit", (1, 256 * 1024**2))
def test_cache_reads_two_partitions_once_and_preserves_batches(tmp_path, monkeypatch, limit):
    monkeypatch.setattr(batch_cache, "TCN_BATCH_CACHE_BYTES", limit)
    contract = V5_TRAINING_PROFILE.heads[0]
    width = max(contract.feature_positions) + 1
    dates = tuple(date(2018, 1, 1) + timedelta(days=i) for i in range(2000))
    split = BuildTrainingDateSplit(dates, parent_manifest_hash="a" * 64, fit_dates=750, history_sessions=62)
    with SQLiteTrainingSampleIndex(tmp_path / "samples.sqlite3", width) as samples:
        samples.add_final(
            TrainingSample(f"{i:06d}", day, "main", "bank", 1e8, tuple(float(j + i) for j in range(width)), (0.01,) * 6)
            for day in (split.model_fit_dates[0], split.early_stopping_dates[0])
            for i in range(5)
        )
        samples.prepare_for_model_fitting(split)
        counts = samples.industry_counts(contract)[0]
        expected = tuple(samples.iter_batches("bank", contract, "training", batch_size=2))
        statements = []
        samples._connection.observe_statements(statements.append)
        with batch_cache.CacheTcnBatches(samples, counts, contract, 2, lambda: None) as cache:
            for _epoch in range(3):
                for actual, original in zip(cache.batches("training"), expected, strict=True):
                    assert actual.features.dtype == np.float64
                    np.testing.assert_array_equal(actual.features, original.features)
                    np.testing.assert_array_equal(actual.labels, original.labels)
            assert bool(tuple(tmp_path.glob("tcn-batches-*"))) == (limit == 1)
        assert len(statements) == 2
        assert not tuple(tmp_path.glob("tcn-batches-*"))

        def cancelled():
            raise InterruptedError("cancelled")

        with pytest.raises(InterruptedError), batch_cache.CacheTcnBatches(samples, counts, contract, 2, cancelled):
            pytest.fail("cancelled cache construction completed")
        assert not tuple(tmp_path.glob("tcn-batches-*"))
