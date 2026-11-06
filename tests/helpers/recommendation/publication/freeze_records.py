from __future__ import annotations
from tests.helpers.recommendation.publication.freeze_identity import NOW, freeze
from trader.infra.cache import BoundedLruCache
from trader.infra.cache_contracts import CacheDatasetPolicy, CacheGroupPolicy, CachePolicy
from trader.recommendation.domain.publication.freeze_identity import CommittedFreezeRecord
from trader.recommendation.domain.publication.models import Strategy


def record(strategy: Strategy = Strategy.TOMORROW, *, sequence: int = 1) -> CommittedFreezeRecord:
    return CommittedFreezeRecord(freeze(strategy, sequence=sequence), NOW, "scheduled")


def _read_cache(*, capacity: int = 72, group_bytes: int = 1_000_000) -> BoundedLruCache[object]:
    policy = CachePolicy(
        datasets={
            dataset: CacheDatasetPolicy(
                86400,
                86400,
                None,
                None,
                60,
                capacity,
                "projection",
                False,
            )
            for dataset in ("published_recommendation_view", "published_date_index")
        },
        groups={"projection": CacheGroupPolicy(group_bytes)},
        total_bytes=group_bytes,
        runtime_reserve_bytes=1024,
        pool_total_bytes=group_bytes + 1024,
        estimator="canonical_json_utf8",
    )
    return BoundedLruCache(policy, wall_clock=lambda: NOW)
