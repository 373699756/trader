from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from trader.application.ports.v2_runtime import V2CycleRequest, V2DecisionUnavailableError
from trader.application.schedule import SHANGHAI
from trader.application.v2_input_runtime import V2MarketDataAdapter
from trader.bootstrap import _recommendation_policy
from trader.domain.recommendation.models import Strategy
from trader.infra.settings import load_strategy_settings

PROJECT_ROOT = Path(__file__).resolve().parents[3]


class _Market:
    def __init__(self, features):
        self._features = tuple(features)

    def fetch_market_features(self, _observed_at, *, force=False):
        del force
        return self._features

    def fetch_candidate_features(
        self,
        codes,
        _observed_at,
        *,
        include_intraday_tail=False,
        include_structured_research=False,
    ):
        del include_intraday_tail, include_structured_research
        requested = set(codes)
        return tuple(feature for feature in self._features if feature.quote.code in requested)


class _LongRuntime:
    def offer_refresh(self, _request):
        return True


def _request(observed_at: datetime, *, phase: str = "close_fallback") -> V2CycleRequest:
    return V2CycleRequest(
        Strategy.TOMORROW,
        observed_at.date(),
        observed_at,
        phase,
        1,
        f"test:{observed_at:%Y%m%dT%H%M%S}",
        False,
        observed_at.replace(hour=14, minute=48),
    )


def test_production_adapter_rejects_transient_invalid_empty_projection(
    application_feature_factory,
) -> None:
    observed_at = datetime(2026, 8, 12, 15, 5, tzinfo=SHANGHAI)
    stale = application_feature_factory("600001", observed_at - timedelta(minutes=1))
    adapter = V2MarketDataAdapter(
        _Market((stale,)),
        config_version="test-config",
        candidate_pool_size=1,
        long_runtime=_LongRuntime(),
        policy=_policy(),
    )
    request = _request(observed_at)

    adapter.refresh(request)

    with pytest.raises(V2DecisionUnavailableError, match="transient_invalid_empty"):
        adapter.build_local(request)


def test_production_adapter_preserves_publishable_business_empty_projection(
    application_feature_factory,
) -> None:
    observed_at = datetime(2026, 8, 12, 14, 40, tzinfo=SHANGHAI)
    feature = application_feature_factory("600001", observed_at - timedelta(seconds=1))
    blocked = replace(feature, quote=replace(feature.quote, is_st=True))
    adapter = V2MarketDataAdapter(
        _Market((blocked,)),
        config_version="test-config",
        candidate_pool_size=1,
        long_runtime=_LongRuntime(),
        policy=_policy(),
    )
    request = _request(observed_at, phase="afternoon")

    adapter.refresh(request)
    decision = adapter.build_local(request)

    assert decision is not None
    assert decision.items == ()


def _policy():
    return _recommendation_policy(load_strategy_settings(PROJECT_ROOT / "config" / "v2" / "strategy.json"))
