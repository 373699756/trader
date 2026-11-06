from __future__ import annotations
from datetime import datetime
from pathlib import Path
from tests.helpers.recommendation.cycle_fixture import CycleFixture, CycleFixtureDependencies
from tests.helpers.recommendation.pipeline_helpers import observed_market_batch
from trader.recommendation.infra.settings.loading import LoadStrategySettings
from trader.recommendation.application.pipeline.freeze_publish.draft_index import UnifiedFreezeDraftIndex
from trader.recommendation.application.ports.runtime import CycleRequest, PipelineTaskRequest
from trader.recommendation.application.runtime.cadence import PipelineTask
from trader.recommendation.application.runtime.schedule import SHANGHAI
from trader.recommendation.domain.market.refresh import ResearchRefreshResult
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.infra.policy_settings import _recommendation_policy

PROJECT_ROOT = Path(__file__).resolve().parents[4]


def _pipeline_facet(status, stage_key: str, facet_key: str) -> int:
    stage = status.pipeline.stage(stage_key)
    return next(item.count for item in stage.facets if item.key == facet_key)


TEST_NOW = datetime(2026, 8, 12, 14, 0, tzinfo=SHANGHAI)


class _Market:
    def prepare_scoring_features(self, features, observed_at, *, include_intraday_tail):
        if not include_intraday_tail:
            return features
        return self.read_candidate_features(
            tuple(feature.quote.code for feature in features), observed_at,
            include_intraday_tail=include_intraday_tail, include_structured_research=True,
        )

    def __init__(self, features):
        self._features = tuple(features)
        self.market_fetch_count = 0
        self.candidate_quote_refresh_count = 0
        self.topk_quote_refresh_count = 0
        self.candidate_reads: list[tuple[tuple[str, ...], bool, bool]] = []
        self.candidate_requests: list[tuple[str, ...]] = []
        self.reference_requests: list[tuple[tuple[str, ...], tuple[str, ...], datetime, bool]] = []
        self.research_requests: list[tuple[str, ...]] = []
        self.requested_codes: tuple[str, ...] = ()

    def fetch_market_features(self, _observed_at, *, force=False, deadline=None):
        del force, deadline
        self.market_fetch_count += 1
        return self._features

    def fetch_market_feature_batch(self, observed_at, *, force=False, deadline=None, round_inputs=None):
        features = tuple(self.fetch_market_features(observed_at, force=force, deadline=deadline))
        return observed_market_batch(features, observed_at)

    def reference_version(self):
        return "reference:fixture"

    def risk_universe_codes(self, _observed_at):
        return tuple(feature.quote.code for feature in self._features if not feature.quote.is_st)

    def refresh_stock_risk(self, codes, observed_at, *, deadline=None):
        del deadline
        requested = tuple(codes)
        self.research_requests.append(requested)
        return ResearchRefreshResult(
            requested_codes=requested,
            completed_codes=requested,
            changed_codes=requested,
            covered_codes=requested,
            data_version="research:fixture",
            started_at=observed_at,
            completed_at=observed_at,
        )

    def refresh_candidate_quotes(self, codes, _observed_at, *, force=False, deadline=None, round_inputs=None):
        del force, deadline
        self.candidate_quote_refresh_count += 1
        self.requested_codes = tuple(codes)
        self.candidate_requests.append(tuple(codes))
        requested = set(codes)
        return tuple(feature for feature in self._features if feature.quote.code in requested)

    def refresh_topk_quotes(self, codes, _observed_at, *, force=False, deadline=None):
        del force, deadline
        self.topk_quote_refresh_count += 1
        self.requested_codes = tuple(codes)
        requested = set(codes)
        return tuple(feature for feature in self._features if feature.quote.code in requested)

    def schedule_reference_data(
        self,
        codes,
        observed_at,
        *,
        force=False,
        security_master_codes=None,
    ):
        self.reference_requests.append((tuple(codes), tuple(security_master_codes or ()), observed_at, force))

    def read_candidate_features(
        self,
        codes,
        _observed_at,
        *,
        include_intraday_tail=False,
        include_structured_research=False,
    ):
        self.candidate_reads.append((tuple(codes), include_intraday_tail, include_structured_research))
        requested = set(codes)
        return tuple(feature for feature in self._features if feature.quote.code in requested)

    def refresh_intraday_tail(self, codes, _observed_at, *, deadline=None, round_inputs=None):
        del codes
        raise AssertionError("the local freeze path must not wait for an intraday network refresh")


class _LongRuntime:
    def offer_refresh(self, _request):
        return True


class _RejectingLongRuntime:
    def offer_refresh(self, _request):
        return False


def _freeze_build(
    drafts: UnifiedFreezeDraftIndex | None = None,
    *,
    now=lambda: TEST_NOW.replace(hour=9),
    monotonic=None,
) -> CycleFixtureDependencies:
    return CycleFixtureDependencies(
        _LongRuntime(),
        _policy(),
        drafts or UnifiedFreezeDraftIndex(),
        now,
        **({"monotonic": monotonic} if monotonic is not None else {}),
    )


def _request(
    observed_at: datetime,
    *,
    strategy: Strategy = Strategy.TOMORROW,
    phase: str = "close_fallback",
) -> CycleRequest:
    return CycleRequest(
        strategy,
        observed_at.date(),
        observed_at,
        phase,
        1,
        f"test:{strategy.value}:{observed_at:%Y%m%dT%H%M%S}",
        False,
        observed_at.replace(hour=14, minute=48),
    )


def _prime_scoring_cache(adapter: CycleFixture, observed_at: datetime) -> None:
    adapter.data.refresh_task(PipelineTaskRequest(PipelineTask.FULL_MARKET, observed_at))
    adapter.data.refresh_task(PipelineTaskRequest(PipelineTask.CANDIDATE_QUOTES, observed_at))


def _policy():
    return _recommendation_policy(LoadStrategySettings(PROJECT_ROOT / "config" / "strategy.json"))
