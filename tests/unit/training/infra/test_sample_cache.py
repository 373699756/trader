from datetime import date, timedelta

from trader.download.domain.baostock_daily import build_baostock_training_split
from trader.training.infra.sample_cache import TrainingSampleCache
from trader.training.infra.sample_repository import TrainingSample


def test_complete_cache_reopens_read_only_and_input_change_rebuilds(tmp_path):
    dates = tuple(date(2020, 1, 1) + timedelta(days=index) for index in range(2000))
    split = build_baostock_training_split(dates, parent_manifest_hash="a" * 64)
    cache = TrainingSampleCache(tmp_path, "first-input", 1, split.content_hash)
    with cache.open() as samples:
        samples.add_final(
            (
                TrainingSample(
                    "600001", split.model_fit_dates[0], "main", "bank", 1.0, (1.0,), (0.1, None, None, None, None, None)
                ),
            )
        )
        samples.prepare_for_model_fitting(split)
        cache.seal(samples)
    with cache.open() as samples:
        assert cache.reused and samples.count() == 1
        samples.require_split(split)
    with cache.path.open("ab") as handle:
        handle.write(b"corrupted-tail")
    with cache.open() as samples:
        assert not cache.reused and samples.count() == 0
    changed = TrainingSampleCache(tmp_path, "changed-input", 1, split.content_hash)
    with changed.open() as samples:
        assert not changed.reused and samples.count() == 0
    changed.clear()
    assert not changed.path.exists()


def test_partial_cache_is_never_reused(tmp_path):
    cache = TrainingSampleCache(tmp_path, "same-input", 1, "split")
    with cache.open():
        pass
    with cache.open() as samples:
        assert not cache.reused and samples.count() == 0
