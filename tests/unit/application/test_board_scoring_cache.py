from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from zoneinfo import ZoneInfo

from trader.application import board_scoring_cache
from trader.application.board_scoring_cache import BoardScoringCache, ScoringCacheContext
from trader.application.cache import (
    CacheDatasetPolicy,
    CacheGroupPolicy,
    CacheIdentity,
    CacheIdentitySpec,
    CachePolicy,
)
from trader.domain.market.models import Board
from trader.domain.recommendation.models import BoardStrategyPolicy, Strategy
from trader.infra.cache import BoundedLruCache

NOW = datetime(2026, 7, 16, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


def _cache() -> BoardScoringCache:
    datasets = {
        name: CacheDatasetPolicy(600, 600, None, None, 60, capacity, "scoring", False)
        for name, capacity in {
            "board_cross_section": 24,
            "competition_group_mapping": 2,
            "candidate_preselection": 36,
        }.items()
    }
    policy = CachePolicy(
        6,
        "v17",
        datasets,
        {"scoring": CacheGroupPolicy(10_000_000)},
        10_000_000,
        1,
        10_000_001,
        "json",
    )
    return BoardScoringCache(BoundedLruCache(policy), config_version="runtime-v16")


def test_cross_section_cache_identity_isolated_by_board_epoch_and_schema(application_feature_factory) -> None:
    cache = _cache()
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    trade_date = now.date().isoformat()
    main = application_feature_factory("600001", now)
    main = replace(main, quote=replace(main.quote, board=Board.MAIN), merge_epoch="epoch-1")
    first = cache.cross_section(
        Board.MAIN, (main,), ScoringCacheContext(trade_date, "today_main", "epoch-1", "data-1", now)
    )
    hot = cache.cross_section(
        Board.MAIN, (main,), ScoringCacheContext(trade_date, "today_main", "epoch-1", "data-1", now)
    )
    next_epoch = cache.cross_section(
        Board.MAIN,
        (replace(main, merge_epoch="epoch-2"),),
        ScoringCacheContext(trade_date, "today_main", "epoch-2", "data-1", now),
    )

    assert hot.population == first.population
    assert hot.reference_values == first.reference_values
    assert hot.features == ()
    assert next_epoch is not first
    assert next_epoch.merge_epoch == "epoch-2"


def test_cross_section_identity_does_not_reserialize_the_full_population(
    application_feature_factory,
    monkeypatch,
) -> None:
    cache = _cache()
    identity_requests: list[dict[str, object]] = []
    original_build_identity = board_scoring_cache.build_cache_identity

    def capture_identity(spec: CacheIdentitySpec) -> CacheIdentity:
        identity_requests.append(dict(spec.request))
        return original_build_identity(spec)

    monkeypatch.setattr(board_scoring_cache, "build_cache_identity", capture_identity)
    main = application_feature_factory("600001", NOW)
    main = replace(main, quote=replace(main.quote, board=Board.MAIN), merge_epoch="epoch-1")

    result = cache.cross_section(
        Board.MAIN,
        (main,),
        ScoringCacheContext("2026-07-16", "today_main", "epoch-1", "data-1", NOW),
    )

    assert result.merge_epoch == "epoch-1"
    assert "feature_version" not in identity_requests[0]


def test_candidate_batch_cache_stores_codes_and_projects_current_features(
    application_feature_factory,
    monkeypatch,
) -> None:
    cache = _cache()
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    identity_requests: list[dict[str, object]] = []
    original_build_identity = board_scoring_cache.build_cache_identity

    def capture_identity(spec: CacheIdentitySpec) -> CacheIdentity:
        identity_requests.append(dict(spec.request))
        return original_build_identity(spec)

    monkeypatch.setattr(board_scoring_cache, "build_cache_identity", capture_identity)
    policy = BoardStrategyPolicy(
        policy_id="candidate-cache-test",
        version="v16",
        board=Board.MAIN,
        strategy=Strategy.TODAY,
        candidate_weights={
            "liquidity": 0.4,
            "intraday_structure": 0.3,
            "turnover_state": 0.2,
            "data_completeness": 0.1,
        },
        local_weights={
            "intraday_structure": 0.4,
            "turnover_state": 0.2,
            "liquidity_execution": 0.2,
            "stability": 0.2,
        },
        candidate_min_score=50.0,
    )
    first = application_feature_factory("600001", now)
    first = replace(first, quote=replace(first.quote, board=Board.MAIN), merge_epoch="epoch-1")
    current = replace(first, missing_reasons={"research": "temporarily_unavailable"})
    context = ScoringCacheContext(now.date().isoformat(), "today_main", "epoch-1", "data-1", now)

    cold = cache.candidate_batch(policy, context, (first,), lambda: (first,))
    hot = cache.candidate_batch(
        policy,
        context,
        (current,),
        lambda: (_ for _ in ()).throw(AssertionError("candidate cache missed")),
    )

    assert cold == (first,)
    assert hot[0] is current
    assert "feature_version" not in identity_requests[-1]
