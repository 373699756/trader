from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from zoneinfo import ZoneInfo

from trader.application.board_scoring_cache import BoardScoringCache, ScoringCacheContext
from trader.application.cache import CacheDatasetPolicy, CacheGroupPolicy, CachePolicy
from trader.domain.models import Board
from trader.infrastructure.cache import BoundedLruCache

NOW = datetime(2026, 7, 16, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


def _cache() -> BoardScoringCache:
    datasets = {
        name: CacheDatasetPolicy(600, 600, None, None, 60, capacity, "scoring", False)
        for name, capacity in {
            "board_cross_section": 24,
            "competition_group_mapping": 2,
            "candidate_preselection": 36,
            "local_score": 1080,
        }.items()
    }
    policy = CachePolicy("v16", datasets, {"scoring": CacheGroupPolicy(10_000_000)}, 10_000_000, "json")
    return BoardScoringCache(BoundedLruCache(policy), config_version="runtime-v16")


def test_cross_section_cache_identity_isolated_by_board_epoch_and_schema(application_feature_factory) -> None:
    cache = _cache()
    main = application_feature_factory("600001", NOW)
    main = replace(main, quote=replace(main.quote, board=Board.MAIN), merge_epoch="epoch-1")
    first = cache.cross_section(
        Board.MAIN, (main,), ScoringCacheContext("2026-07-16", "today_main", "epoch-1", "data-1", NOW)
    )
    hot = cache.cross_section(
        Board.MAIN, (main,), ScoringCacheContext("2026-07-16", "today_main", "epoch-1", "data-1", NOW)
    )
    next_epoch = cache.cross_section(
        Board.MAIN,
        (replace(main, merge_epoch="epoch-2"),),
        ScoringCacheContext("2026-07-16", "today_main", "epoch-2", "data-1", NOW),
    )

    assert hot == first
    assert next_epoch is not first
    assert next_epoch.merge_epoch == "epoch-2"
