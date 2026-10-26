from dataclasses import replace
from datetime import date, timedelta

import numpy as np
import pytest

from trader.training.domain.training_dates import BuildTrainingDateSplit
from trader.training.infra import sample_sequences
from trader.training.infra.profile.v5.contracts import V5_TRAINING_PROFILE
from trader.training.infra.sample_cache import TrainingSampleCache
from trader.training.infra.sample_index import SQLiteTrainingSampleIndex, TrainingSample
from trader.training.infra.sample_sequences import SampleSequenceLayout

LAYOUT = SampleSequenceLayout(6, 60, 6)


def _split():
    return BuildTrainingDateSplit(
        tuple(date(2020, 1, 1) + timedelta(days=i) for i in range(2000)), parent_manifest_hash="a" * 64
    )


def _samples(days, codes=4):
    values = np.random.default_rng(31).normal(size=(codes, len(days) + 60, 6))
    for day_number, day in enumerate(days):
        batch = []
        for code in range(codes):
            if code == 0 and day_number % 7 == 1:
                continue
            features = (
                *tuple(float(code + index + day_number / 100) for index in range(6)),
                *values[code, day_number : day_number + 60].ravel(),
            )
            targets = (None if day_number % 5 == 1 else 0.1, 0.2, 0.3, 0.4, 0.5, None if day_number % 5 == 2 else 0.35)
            batch.append(TrainingSample(f"{code:06d}", day, "main", f"industry{code % 2}", 1e7, features, targets))
        yield tuple(batch)


def test_compact_samples_match_all_head_partitions_statistics_and_order(tmp_path):
    split = _split()
    days = (
        *split.model_fit_dates[:150],
        *split.early_stopping_dates[:4],
        *split.calibration_dates[:3],
        *split.confirmation_dates[:3],
        *split.daily_proxy_holdout_dates[:2],
    )
    with (
        SQLiteTrainingSampleIndex(tmp_path / "wide.sqlite3", 366) as wide,
        SQLiteTrainingSampleIndex(tmp_path / "compact.sqlite3", 366, sequence_layout=LAYOUT) as compact,
    ):
        for batch in _samples(days):
            wide.add_final(batch)
            compact.add_final(batch)
        for samples in (wide, compact):
            samples.prepare_for_model_fitting(split)
        assert wide.count() == compact.count()
        assert wide.validation_target_metrics() == compact.validation_target_metrics()
        contracts = (
            *V5_TRAINING_PROFILE.heads,
            replace(V5_TRAINING_PROFILE.heads[0], feature_positions=(365, 2, 0, 4)),
        )
        for contract in contracts:
            assert wide.industry_counts(contract) == compact.industry_counts(contract)
            assert wide.coverage(contract) == compact.coverage(contract)
            for partition in ("training", "early_stopping", "calibration", "validation"):
                for industry in ("industry0", "industry1"):
                    expected = tuple(wide.iter_batches(industry, contract, partition, batch_size=7))
                    observed = tuple(compact.iter_batches(industry, contract, partition, batch_size=7))
                    assert len(observed) == len(expected)
                    for left, right in zip(expected, observed, strict=True):
                        np.testing.assert_array_equal(left.features, right.features)
                        np.testing.assert_array_equal(left.labels, right.labels)
                        assert left.trade_dates == right.trade_dates and left.codes == right.codes
                        assert left.industries == right.industries and right.features.dtype == np.float64
            count = compact.industry_counts(contract)[0]
            np.testing.assert_array_equal(
                wide.industry_data(count, contract).training.features,
                compact.industry_data(count, contract).training.features,
            )
            with pytest.raises(ValueError, match="holdout_is_sealed"):
                tuple(compact.iter_batches("industry0", contract, "holdout"))
            expected = tuple(wide.iter_validation_batches(contract))
            observed = tuple(compact.iter_validation_batches(contract))
            for left, right in zip(expected, observed, strict=True):
                np.testing.assert_array_equal(left.features, right.features)
                assert left.codes == right.codes
        assert compact.database_size_bytes < wide.database_size_bytes * 0.25


def test_eviction_partial_blocks_and_large_batch_keep_exact_values(tmp_path, monkeypatch):
    monkeypatch.setattr(sample_sequences, "_WRITER_CODES", 2)
    monkeypatch.setattr(sample_sequences, "_READER_BYTES", 3072)
    split = _split()
    all_samples = tuple(item for batch in _samples(split.model_fit_dates[:70]) for item in batch)
    contract = V5_TRAINING_PROFILE.heads[0]
    with SQLiteTrainingSampleIndex(tmp_path / "samples.sqlite3", 366, sequence_layout=LAYOUT) as samples:
        samples.add_final(all_samples)
        samples.prepare_for_model_fitting(split)
        queries = []
        samples._connection.observe_statements(queries.append)
        batches = tuple(samples.iter_batches("industry1", contract, "training"))
        expected = [item for item in all_samples if item.industry == "industry1" and item.targets[0] is not None]
        np.testing.assert_array_equal(
            np.concatenate([batch.features for batch in batches]), np.asarray([item.features for item in expected])
        )
        assert sum("WITH wanted" in query for query in queries) <= 2


@pytest.mark.parametrize(
    "corruption", ["DELETE FROM sample_sequence_blocks", "UPDATE sample_sequence_blocks SET payload=x'00'"]
)
def test_missing_or_truncated_blocks_are_rejected(tmp_path, corruption):
    split = _split()
    with SQLiteTrainingSampleIndex(tmp_path / "samples.sqlite3", 366, sequence_layout=LAYOUT) as samples:
        samples.add_final(next(_samples(split.model_fit_dates[:1])))
        samples.prepare_for_model_fitting(split)
        samples._connection.run(corruption)
        with pytest.raises(ValueError):
            tuple(samples.iter_batches("industry1", V5_TRAINING_PROFILE.heads[0], "training"))


def test_reused_sequence_blocks_are_immutable_and_keep_the_cache_byte_limit(tmp_path, monkeypatch):
    split = _split()
    with SQLiteTrainingSampleIndex(tmp_path / "samples.sqlite3", 366, sequence_layout=LAYOUT) as samples:
        samples.add_final(item for batch in _samples(split.model_fit_dates[:6]) for item in batch)
        samples.prepare_for_model_fitting(split)
        reader = sample_sequences.SampleSequenceReader(samples._connection, LAYOUT)
        windows = (("000001", 0), ("000001", 5))
        expected = reader.read(windows)
        for vector in reader._blocks.values():
            assert not vector.flags.writeable
            with pytest.raises(ValueError, match="read-only"):
                vector[0] = 1.0
        queries = []
        samples._connection.observe_statements(queries.append)
        returned = reader.read(windows)
        returned.fill(np.nan)
        np.testing.assert_array_equal(reader.read(windows), expected)
        assert not queries
        monkeypatch.setattr(sample_sequences, "_READER_BYTES", 3072)
        np.testing.assert_array_equal(reader.read(windows), expected)
        assert reader._bytes <= 3072
        np.testing.assert_array_equal(reader.read(windows), expected)
        assert any("WITH wanted" in query for query in queries)


def test_compact_cache_reopens_and_layout_change_rebuilds_with_same_input_identity(tmp_path):
    split = _split()
    cache = TrainingSampleCache(tmp_path, "same", 366, split.content_hash, sequence_layout=LAYOUT)
    with cache.open() as samples:
        samples.add_final(next(_samples(split.model_fit_dates[:1])))
        samples.prepare_for_model_fitting(split)
        cache.seal(samples)
    with cache.open() as samples:
        assert cache.reused and samples.count() == 4
        assert next(samples.iter_batches("industry1", V5_TRAINING_PROFILE.heads[0], "training")).features.shape == (
            2,
            366,
        )
    columns = TrainingSampleCache(tmp_path, "same", 366, split.content_hash)
    with columns.open() as samples:
        assert not columns.reused and samples.count() == 0


def test_nonfinite_sequence_is_rejected_before_sealing(tmp_path):
    split = _split()
    sample = next(_samples(split.model_fit_dates[:1]))[0]
    invalid = replace(sample, features=(*sample.features[:-1], float("nan")))
    with SQLiteTrainingSampleIndex(tmp_path / "samples.sqlite3", 366, sequence_layout=LAYOUT) as samples:
        with pytest.raises(ValueError, match="values are invalid"):
            samples.add_final((invalid,))


def test_full_batch_reads_two_blocks_per_code_with_portable_parameter_bound(tmp_path):
    split = _split()
    values = np.random.default_rng(7).normal(size=(256, 65, 6))
    with SQLiteTrainingSampleIndex(tmp_path / "samples.sqlite3", 366, sequence_layout=LAYOUT) as samples:
        for offset, day in enumerate(split.model_fit_dates[:6]):
            samples.add_final(
                tuple(
                    TrainingSample(
                        f"{code:06d}",
                        day,
                        "main",
                        "industry",
                        1e7,
                        (*((0.0,) * 6), *values[code, offset : offset + 60].ravel()),
                        (0.1, 0.2, 0.3, 0.4, 0.5, 0.35),
                    )
                    for code in range(256)
                )
            )
        samples.prepare_for_model_fitting(split)
        queries = []
        samples._connection.observe_statements(queries.append)
        reader = sample_sequences.SampleSequenceReader(samples._connection, LAYOUT)
        decoded = reader.read(tuple((f"{code:06d}", 5) for code in range(256)))
        np.testing.assert_array_equal(decoded, values[:, 5:65].reshape(256, -1))
        assert sum("WITH wanted" in query for query in queries) == 2
