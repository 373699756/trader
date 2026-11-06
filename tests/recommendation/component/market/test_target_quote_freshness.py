from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
import threading
import time
from zoneinfo import ZoneInfo

import pytest

from tests.helpers.recommendation.market_data import FailingMarketClient, FakeSession, _quote
from tests.helpers.recommendation.publication.tomorrow_projection import (
    _ProductionPredictor,
    _with_model_features,
)
from tests.helpers.recommendation.runtime.input_runtime import _policy
from tests.helpers.recommendation.runtime.tomorrow_freezing import _Clock
from tests.helpers.recommendation.scoring_helpers import profile_for
from tests.helpers.shared.configuration import SqlitePolicyFixture
from trader.infra.http_contracts import HttpConnectionError, HttpTimeout
from trader.recommendation.application.pipeline.final_selection.freeze_projection import (
    BuildScoredLocal,
    ScoredProjectionInputs,
)
from trader.recommendation.application.pipeline.freeze_publish.freeze_coordinator import (
    FreezeRuntimeIdentity,
    ScoredFreezeCoordinator,
)
from trader.recommendation.application.pipeline.freeze_publish.snapshot_publisher import UnifiedFreezeIndex
from trader.recommendation.application.pipeline.local_score.model_router import ModelScoringRouter
from trader.recommendation.application.pipeline.local_score.model_scoring import ProductionModelScoringService
from trader.recommendation.application.ports.scoring import D25NativeInput, TomorrowNativeInput
from trader.recommendation.application.ports.market_data import MarketDataDeadlineExceededError
from trader.recommendation.domain.candidate.filters import HardFilter
from trader.recommendation.domain.publication.models import RecommendationAction
from trader.recommendation.domain.selection.execution_cost import ExecutionCostPolicy
from trader.recommendation.infra.market_data.gateway import MarketDataGateway
from trader.recommendation.infra.market_data.providers.sina import SinaClient
from trader.recommendation.infra.market_data.providers.tencent import TencentClient
from trader.recommendation.infra.normalization.merge import ObservationFromQuote
from trader.recommendation.infra.persistence.freeze_records import SQLiteFreezeRecords

_OBSERVED_AT = datetime(2026, 7, 16, 14, 50, tzinfo=ZoneInfo("Asia/Shanghai"))
_STALE_AT = _OBSERVED_AT.replace(hour=10, minute=0)
_CODE = "600001"


def _sina_payload(source_time):
    return {
        "symbol": f"sh{_CODE}", "name": "测试股份", "trade": "12.00", "settlement": "11.65",
        "open": "11.80", "high": "12.20", "low": "11.70", "changepercent": "3.00",
        "turnoverratio": "3.0", "amount": "300000000", "volume": "25000000", "mktcap": "3000000",
        "day": source_time.date().isoformat(), "ticktime": source_time.strftime("%H:%M:%S"),
    }


def _tencent_payload(code=_CODE, *, price="12.00", source_time=_OBSERVED_AT):
    fields = [""] * 50
    for index, value in {
        1: "测试股份", 2: code, 3: price, 4: "11.65", 5: "11.80", 6: "250000",
        30: source_time.strftime("%Y%m%d%H%M%S") if source_time is not None else "",
        32: "3.00", 33: "12.20", 34: "11.70",
        35: "0/0/300000000", 38: "3.0", 43: "4.0", 45: "300.0", 49: "2.0",
    }.items():
        fields[index] = value
    return f'v_sh{code}="{"~".join(fields)}";'.encode("gb18030")


def _gateway(source_time, outcome, *, target_price="12.00", target_time=_OBSERVED_AT, monotonic=time.monotonic):
    sina_session = FakeSession((b"1", [_sina_payload(source_time)]))
    response = {
        "empty": b"", "failure": HttpConnectionError("controlled_target_failure"),
        "timeout": HttpTimeout("controlled_target_timeout"), "missing": _tencent_payload("600002"),
        "fresh": _tencent_payload(),
        "invalid": _tencent_payload(price=target_price, source_time=target_time),
    }[outcome]
    tencent_session = FakeSession((response,) * 4)
    gateway = MarketDataGateway(
        FailingMarketClient(),
        SinaClient(
            worker_pool=None, timeout_seconds=1, session_factory=lambda: sina_session,
            wall_clock=lambda: _OBSERVED_AT,
        ),
        TencentClient(
            worker_pool=None, timeout_seconds=1, session_factory=lambda: tencent_session,
            wall_clock=lambda: _OBSERVED_AT,
        ),
        minimum_market_rows=1, circuit_breaker_failures=1, circuit_breaker_seconds=60,
        wall_clock=lambda: _OBSERVED_AT, monotonic=monotonic,
    )
    reference_quote = replace(_quote(_CODE), source_time=_OBSERVED_AT, received_time=_OBSERVED_AT)
    reference = replace(
        ObservationFromQuote(reference_quote, source="exchange", observed_at=_OBSERVED_AT),
        fields={
            "board": "main", "exchange": "SSE", "listing_date": "2020-01-01", "listing_age_sessions": 1000.0,
        },
    )
    gateway.update_reference_observations((reference,))
    # Keep the full-market quote as display/discovery input. Eligibility must
    # still use supplier time when the directed collection cannot replace it.
    market = tuple(gateway.fetch_market())
    return gateway, market, tencent_session


def _project_and_freeze(quotes, native_type, feature_factory, tmp_path):
    features = tuple(
        _with_model_features(
            replace(feature_factory(quote.code, _OBSERVED_AT), quote=replace(quote, industry="工业")), index,
        )
        for index, quote in enumerate(quotes)
    )
    strategy = native_type.strategy
    policy = _policy()
    scorer = ProductionModelScoringService(
        profile_for(_ProductionPredictor(), strategy), strategy, cost_policy=ExecutionCostPolicy(0.002, 1.0),
    )
    native = native_type(
        trade_date=_OBSERVED_AT.date(), phase="final_review", data_version="target-freshness:fixture",
        config_version="target-freshness:config", evaluated_at=_OBSERVED_AT, market_features=features,
        requested_codes=(_CODE,), candidate_features=features,
        preselect_max_age_seconds=30, score_max_age_seconds=30,
    )
    projection = BuildScoredLocal(
        native, policy, sequence=1,
        runtime=ScoredProjectionInputs(model_scoring=ModelScoringRouter("v2", {strategy: scorer})),
    )
    index = UnifiedFreezeIndex()
    assert index.publish(projection.local, expected_version=None).accepted
    records = SQLiteFreezeRecords(tmp_path / "freeze.sqlite3", sqlite_policy=SqlitePolicyFixture())
    records.initialize()
    freeze = projection.local
    coordinator = ScoredFreezeCoordinator(
        index, records, _Clock(_OBSERVED_AT.replace(hour=15, minute=0)), strategy=strategy,
        runtime_identity=FreezeRuntimeIdentity(freeze.config_version, freeze.strategy_version, freeze.fusion_version),
    )
    result = coordinator.freeze_scheduled()
    assert result.status == "frozen" and result.record is not None
    assert records.load(strategy, _OBSERVED_AT.date()) == result.record
    return features, projection, result.record


@pytest.mark.parametrize("native_type", (TomorrowNativeInput, D25NativeInput))
@pytest.mark.parametrize("outcome", ("empty", "failure", "timeout", "missing"))
def test_stale_sina_after_failed_target_refresh_remains_ineligible_through_formal_freeze(
    application_feature_factory, tmp_path, native_type, outcome,
):
    gateway, market, session = _gateway(_STALE_AT, outcome)
    quotes = tuple(gateway.fetch_candidates((_CODE,), force=True))
    features, projection, record = _project_and_freeze(quotes, native_type, application_feature_factory, tmp_path)

    assert len(market) == len(quotes) == len(features) == 1
    assert session.calls, "the regression must exercise the real targeted Tencent parser/transport boundary"
    assert projection.selection.scored_candidates == ()
    assert all(item.action is not RecommendationAction.EXECUTABLE for item in record.freeze.items)
    for feature in features:
        assert feature.quote.source_time == _STALE_AT
        assert feature.quote.received_time == _OBSERVED_AT
        assert feature.quote.age_seconds(_OBSERVED_AT) == 17_400
        filtered = HardFilter(feature, _OBSERVED_AT, max_age_seconds=30, policy=_policy().hard_filter)
        assert not filtered.allowed
        assert "stale_quote" in {reason.code for reason in (*filtered.reasons, *filtered.deferred)}
    assert all(quote.source_time == _STALE_AT for quote in market)


@pytest.mark.parametrize("outcome", ("empty", "failure", "timeout", "missing"))
def test_still_fresh_failed_target_refresh_keeps_original_time_and_restricts_execution(
    application_feature_factory, tmp_path, outcome,
):
    source_time = _OBSERVED_AT - timedelta(seconds=15)
    gateway, _market, _session = _gateway(source_time, outcome)
    quotes = tuple(gateway.fetch_candidates((_CODE,), observed_at=_OBSERVED_AT, force=True))
    assert len(quotes) == 1
    assert quotes[0].source_time == source_time
    assert quotes[0].age_seconds(_OBSERVED_AT) == 15
    assert "market_data_degraded" in quotes[0].execution_restrictions
    _features, projection, record = _project_and_freeze(
        quotes, TomorrowNativeInput, application_feature_factory, tmp_path,
    )
    assert projection.selection.scored_candidates
    assert record.freeze.items
    assert all(item.action is not RecommendationAction.EXECUTABLE for item in record.freeze.items)


def test_successful_fresh_target_refresh_uses_real_supplier_time_and_remains_score_eligible(
    application_feature_factory, tmp_path,
):
    gateway, _market, _session = _gateway(_STALE_AT, "fresh")
    quotes = tuple(gateway.fetch_candidates((_CODE,), observed_at=_OBSERVED_AT, force=True))
    assert len(quotes) == 1 and quotes[0].source == "tencent"
    assert quotes[0].source_time == quotes[0].received_time == _OBSERVED_AT
    assert "market_data_degraded" not in quotes[0].execution_restrictions
    _features, projection, record = _project_and_freeze(
        quotes, TomorrowNativeInput, application_feature_factory, tmp_path,
    )
    assert projection.selection.scored_candidates
    assert record.freeze.items
    assert record.freeze.items[0].quote is not None
    assert record.freeze.items[0].quote.source_time == _OBSERVED_AT


@pytest.mark.parametrize(
    "price,target_time",
    (
        pytest.param("0", _OBSERVED_AT, id="zero-price"),
        pytest.param("-1", _OBSERVED_AT, id="negative-price"),
        pytest.param("nan", _OBSERVED_AT, id="nonfinite-price"),
        pytest.param("", _OBSERVED_AT, id="missing-price"),
        pytest.param("12.00", _STALE_AT, id="older-target"),
        pytest.param("12.00", _OBSERVED_AT + timedelta(seconds=1), id="future-target"),
        pytest.param("12.00", None, id="unknown-source-time"),
    ),
)
def test_invalid_directed_quote_retains_valid_quote_and_cannot_poison_later_recovery(
    application_feature_factory, tmp_path, price, target_time,
):
    source_time = _OBSERVED_AT - timedelta(seconds=15)
    elapsed = [0.0]
    gateway, market, session = _gateway(
        source_time, "invalid", target_price=price, target_time=target_time, monotonic=lambda: elapsed[0],
    )
    initial_raw = dict(gateway._latest_observations[_CODE])
    retained = tuple(gateway.fetch_candidates((_CODE,), observed_at=_OBSERVED_AT, force=True))

    assert len(retained) == 1
    assert replace(retained[0], execution_restrictions=market[0].execution_restrictions) == market[0]
    assert "market_data_degraded" in retained[0].execution_restrictions
    assert gateway.current_quotes((_CODE,)) == retained
    assert gateway.canonical_snapshot().quotes == retained
    assert gateway._latest_observations[_CODE] == initial_raw
    _features, projection, record = _project_and_freeze(
        retained, TomorrowNativeInput, application_feature_factory, tmp_path,
    )
    assert projection.selection.scored_candidates
    assert all(item.action is not RecommendationAction.EXECUTABLE for item in record.freeze.items)

    # Only a valid subsequent provider result can clear this code's failure.
    # An unknown timestamp is a supplier failure, so allow the configured
    # breaker to expire before the next real provider attempt.
    elapsed[0] = 61.0
    session._payloads = iter((_tencent_payload(),))
    recovered = tuple(gateway.fetch_candidates((_CODE,), observed_at=_OBSERVED_AT, force=True))
    assert len(recovered) == 1 and recovered[0].price == 12.0
    assert recovered[0].source_time == recovered[0].received_time == _OBSERVED_AT
    assert "market_data_degraded" not in recovered[0].execution_restrictions


def test_cancellation_before_retained_quote_commit_leaves_the_snapshot_unchanged(monkeypatch):
    gateway, market, _session = _gateway(_OBSERVED_AT - timedelta(seconds=15), "failure")
    snapshot = gateway.canonical_snapshot()
    original_lock = gateway._state_lock
    original_retain = gateway._retain_targeted_quotes
    cancelled = threading.Event()
    entered_commit = threading.Event()

    class CancelOnEntry:
        def __enter__(self):
            # Cancellation arrives after the supplier-failure branch's first
            # check, exactly while it acquires the commit lock.
            cancelled.set()
            return original_lock.__enter__()

        def __exit__(self, *args):
            return original_lock.__exit__(*args)

    def retain_after_precheck(*args, **kwargs):
        entered_commit.set()
        monkeypatch.setattr(gateway, "_state_lock", CancelOnEntry())
        return original_retain(*args, **kwargs)

    monkeypatch.setattr(gateway, "_retain_targeted_quotes", retain_after_precheck)
    with pytest.raises(MarketDataDeadlineExceededError, match="round_input_cancelled"):
        gateway.fetch_candidates((_CODE,), observed_at=_OBSERVED_AT, force=True, cancelled=cancelled.is_set)
    assert entered_commit.is_set()
    assert gateway.canonical_snapshot() is snapshot
    assert gateway.current_quotes((_CODE,)) == market
