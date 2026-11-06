"""Enabled-profile unions, failure fanout and release of completed round inputs."""

import gc
import weakref
from concurrent.futures import ThreadPoolExecutor
from itertools import combinations

import pytest

from trader.recommendation.application.runtime.round_inputs import RoundCollection, RoundInputCancelled


@pytest.mark.parametrize("profiles", [part for size in range(1, 5) for part in combinations(("v1", "v2", "v3", "v5"), size)])
def test_all_enabled_subsets_collect_one_union_including_empty_profiles(profiles):
    collection = RoundCollection[tuple[str, ...]](profiles, lambda: 10.0)
    calls = []

    def load(codes):
        calls.append(codes)
        return codes

    def collect(item):
        index, profile = item
        return collection.collect(profile, () if index == 0 else ("600001", f"00000{index}"), load)

    with ThreadPoolExecutor(max_workers=len(profiles)) as pool:
        results = tuple(pool.map(collect, enumerate(profiles)))
    expected = tuple(sorted({code for index in range(1, len(profiles)) for code in ("600001", f"00000{index}")}))
    assert calls == [expected]
    assert all(result is results[0] for result in results)
    assert results[0] == expected


def test_failed_collection_wakes_every_peer_without_issuing_a_second_request():
    collection = RoundCollection[tuple[str, ...]](("v1", "v5"), lambda: 10.0)

    def fail(codes):
        raise OSError("source_failed")

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(collection.collect, profile, (), fail) for profile in ("v1", "v5")]
        failures = [future.exception(timeout=2) for future in futures]
    assert sum(isinstance(error, OSError) for error in failures) == 1
    assert sum(isinstance(error, RoundInputCancelled) for error in failures) == 1


def test_late_result_is_discarded_and_collection_releases_its_payload():
    remaining = [1.0]
    collection = RoundCollection[tuple[str, ...]](("v2",), lambda: remaining[0])

    def late(codes):
        remaining[0] = 0.0
        return codes

    with pytest.raises(RoundInputCancelled):
        collection.collect("v2", ("600001",), late)
    reference = weakref.ref(collection)
    del collection
    gc.collect()
    assert reference() is None
