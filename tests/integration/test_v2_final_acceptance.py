from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import datetime, timedelta

from trader.application.pipeline import RecommendationPipeline
from trader.application.publisher import SnapshotPublisher
from trader.application.recommendations import RecommendationEngine
from trader.application.status import RuntimeState
from trader.domain.models import (
    DeepSeekReview,
    DimensionAssessment,
    Evidence,
    FeatureSnapshot,
    ReviewOutcome,
    Strategy,
)
from trader.entrypoints.cli import main as cli_main
from trader.infrastructure.deepseek.budget import DeepSeekBudgetStore
from trader.infrastructure.deepseek.cache import ReviewCache
from trader.infrastructure.deepseek.client import DeepSeekHttpClient
from trader.infrastructure.deepseek.reviewer import DeepSeekReviewer
from trader.infrastructure.persistence.snapshots import snapshot_bytes, snapshot_from_dict
from trader.infrastructure.persistence.writer import SnapshotRepository
from trader.infrastructure.settings import DeepSeekSettings


def test_frozen_input_round_trip_recomputes_filters_scores_risks_veto_and_ranking(
    tmp_path,
    capsys,
    recommendation_policy,
    application_feature_factory,
) -> None:
    now = datetime.fromisoformat("2026-07-16T10:00:00+08:00")
    accepted = application_feature_factory("600001", now)
    rejected = application_feature_factory("600002", now)
    rejected = replace(rejected, quote=replace(rejected.quote, pct_change=8.01))
    engine = RecommendationEngine(recommendation_policy)
    candidates, reasons = engine.preselect((accepted, rejected), now=now, max_age_seconds=20.0, limit=120)
    reviewer = RecordedReviewer(
        _review(accepted.quote.code, now, recommendation_policy.dimension_weights[Strategy.TODAY])
    )
    snapshot = engine.build_snapshot(
        Strategy.TODAY,
        candidates,
        now=now,
        phase="today_main",
        trade_date="2026-07-16",
        data_version="acceptance-v1",
        review_port=reviewer,
        review_deadline=now + timedelta(hours=1),
        max_age_seconds=20.0,
        filtered_count=1,
        filter_reasons=reasons,
        market_features=(accepted, rejected),
        requested_codes=tuple(feature.quote.code for feature in candidates),
        preselect_max_age_seconds=20.0,
        candidate_pool_size=120,
    )
    assert snapshot.replay_input is not None
    frozen = replace(snapshot, frozen=True)

    restored = snapshot_from_dict(json.loads(snapshot_bytes(frozen)))
    result = RecommendationEngine.verify_frozen(restored)

    assert result == {
        "status": "verified",
        "snapshot_id": snapshot.snapshot_id,
        "strategy": "today",
        "market_input_count": 2,
        "candidate_input_count": 1,
        "recommendation_count": 1,
    }
    assert restored.filter_reasons == {"main_board_too_hot": 1}
    assert restored.recommendations == engine.replay(restored).recommendations
    snapshot_path = (tmp_path / "frozen.json").resolve()
    snapshot_path.write_bytes(snapshot_bytes(frozen))

    assert cli_main(["verify-freeze", "--snapshot", str(snapshot_path)]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "verified"
    assert cli_main(["threshold-report", "--snapshot", str(snapshot_path)]) == 0
    threshold_report = json.loads(capsys.readouterr().out)
    assert threshold_report["schema_version"] == "threshold_report_v1"
    assert threshold_report["strategies"]["today"]["score_distribution"]["count"] == 1
    assert tuple(tmp_path.iterdir()) == (snapshot_path,)


def test_configured_deepseek_candidate_makes_physical_call_and_status_reports_quote_p95(
    tmp_path,
    recommendation_policy,
    application_feature_factory,
) -> None:
    now = datetime.fromisoformat("2026-07-16T10:00:00+08:00")
    feature = application_feature_factory("600001", now)
    feature = replace(
        feature,
        quote=replace(feature.quote, source_time=now - timedelta(seconds=11)),
        evidence=(Evidence("e-1", "structured", "结构化行情", "fixture", now),),
    )
    runtime_dir = tmp_path / "runtime"
    repository = SnapshotRepository(runtime_dir, config_version="acceptance-v2")
    repository.initialize()
    budget = DeepSeekBudgetStore(
        runtime_dir / "runtime.sqlite3",
        daily_hard_limit=2,
        strategy_limits={"today": 2, "tomorrow": 0, "d25": 0, "long": 0, "shared_preheat": 0, "emergency": 0},
    )
    budget.initialize()
    calls = 0

    def post(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        content = json.dumps(_payload(feature.quote.code), ensure_ascii=False)
        return FakeHttpResponse({"choices": [{"message": {"content": content}}], "usage": {"total_tokens": 10}})

    reviewer = DeepSeekReviewer(
        _deepseek_settings(),
        budget,
        DeepSeekHttpClient(post=post, sleep=lambda _seconds: None),
        ReviewCache(),
        now=lambda: now,
    )
    pipeline = RecommendationPipeline(
        StaticMarketData((feature,)),
        TradingDayCalendar(),
        reviewer,
        repository,
        repository,
        SnapshotPublisher(history_size=8, client_queue_size=2),
        RecommendationEngine(recommendation_policy),
        RuntimeState(),
        config_version="acceptance-v2",
        candidate_pool_size=120,
        event_queue_size=8,
        priority_queue_size=2,
        now=lambda: now,
    )
    pipeline.initialize()

    snapshots = pipeline.run_once(now)
    status = pipeline.status()
    market_status = status["dependencies"]["market_data"]
    deepseek_status = status["dependencies"]["deepseek"]

    assert snapshots
    assert calls == 1
    assert deepseek_status["physical_call_acceptance"] == {
        "applicable": True,
        "passed": True,
        "physical_attempts_last_batch": 1,
        "zero_call_reason": "",
    }
    assert market_status["topk_quote_age"]["sample_count"] == 1
    assert market_status["topk_quote_age"]["p95_seconds"] == 11.0
    assert market_status["topk_quote_age"]["meets_target"] is False


class RecordedReviewer:
    def __init__(self, review: DeepSeekReview) -> None:
        self._review = review

    def review(
        self,
        _strategy: Strategy,
        candidates: Sequence[FeatureSnapshot],
        *,
        phase: str,
        deadline: datetime,
    ) -> Mapping[str, DeepSeekReview]:
        del phase, deadline
        return {candidate.quote.code: self._review for candidate in candidates}

    def preheat(
        self,
        candidates: Sequence[FeatureSnapshot],
        *,
        phase: str,
        deadline: datetime,
    ) -> Mapping[str, DeepSeekReview]:
        return self.review(Strategy.TODAY, candidates, phase=phase, deadline=deadline)

    @staticmethod
    def status() -> Mapping[str, object]:
        return {}


class StaticMarketData:
    def __init__(self, features: Sequence[FeatureSnapshot]) -> None:
        self._features = tuple(features)

    def fetch_market_features(self, _observed_at: datetime) -> Sequence[FeatureSnapshot]:
        return self._features

    def fetch_candidate_features(
        self,
        codes: Sequence[str],
        _observed_at: datetime,
    ) -> Sequence[FeatureSnapshot]:
        requested = set(codes)
        return tuple(feature for feature in self._features if feature.quote.code in requested)

    @staticmethod
    def health() -> Mapping[str, object]:
        return {"status": "fixture"}


class TradingDayCalendar:
    @staticmethod
    def is_trading_day(_day) -> bool:
        return True


class FakeHttpResponse:
    status_code = 200
    headers: Mapping[str, str] = {}

    def __init__(self, payload: object) -> None:
        self._payload = payload

    @staticmethod
    def raise_for_status() -> None:
        return None

    def json(self) -> object:
        return self._payload


def _review(code: str, now: datetime, weights: Mapping[str, float]) -> DeepSeekReview:
    dimensions = {name: DimensionAssessment(name, 80.0, 1.0, "positive", evidence_ids=()) for name in weights}
    return DeepSeekReview(code, ReviewOutcome.APPLIED, dimensions, (), now)


def _payload(code: str) -> dict[str, object]:
    dimensions = {
        name: {
            "score": 80,
            "confidence": 1.0,
            "assessment": "positive",
            "flags": [],
            "evidence_ids": ["e-1"],
            "unknown": False,
        }
        for name in ("value_quality", "financial_health", "market_flow", "industry_policy", "risk_quality")
    }
    return {"results": [{"code": code, "abstain": False, "dimensions": dimensions, "risk_facts": []}]}


def _deepseek_settings() -> DeepSeekSettings:
    return DeepSeekSettings(
        enabled=True,
        base_url="https://api.deepseek.example/v1",
        model="model",
        timeout_seconds=1.0,
        batch_size=8,
        max_tokens=256,
        confidence_coverage_min=0.5,
        daily_hard_limit=2,
        strategy_limits={"today": 2, "tomorrow": 0, "d25": 0, "long": 0, "shared_preheat": 0, "emergency": 0},
        api_key="secret",
    )
