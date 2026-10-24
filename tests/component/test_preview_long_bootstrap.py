from __future__ import annotations

from dataclasses import replace

import pytest

from tests.contract.test_bootstrap import _config
from tests.unit.domain.test_preview_identity import MARKET_DATE, STARTED_AT
from trader.bootstrap import BuildProfiledSystem
from trader.infra.shutdown import ShutdownDeadline
from trader.recommendation.domain.market.eligibility import IssuerEligibilityFact, IssuerEligibilityReason
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.infra.scoring.profile_factory import ScoringModelsMissingError


@pytest.mark.parametrize("profiles", (("v1",), ("v2",), ("v3",), ("v5",), ("v1", "v2", "v3", "v5"), ("v3", "v5")))
@pytest.mark.parametrize("quote_available", (False, True))
def test_composed_preview_long_publishes_without_formal_database_or_models(
    tmp_path, monkeypatch, application_feature_factory, profiles, quote_available
):
    close = STARTED_AT.replace(day=23, hour=15)
    calls = []

    def missing_model(*args, **kwargs):
        raise ScoringModelsMissingError()

    def quotes(self, codes, observed_at, **kwargs):
        calls.append((tuple(codes), kwargs["preview_context"]))
        if not quote_available:
            return ()
        return tuple(
            replace(application_feature_factory(code, observed_at).quote, source_time=close, pct_change=1.75)
            for code in codes
        )

    monkeypatch.setattr("trader.bootstrap._utc_now", lambda: STARTED_AT)
    monkeypatch.setattr("trader.bootstrap.LoadScoringProfile", missing_model)
    monkeypatch.setattr("trader.bootstrap.PublishedReferences.initialize", lambda self: None)
    monkeypatch.setattr("trader.bootstrap.ChinaTradingCalendar.open_dates", lambda self: (MARKET_DATE,))
    monkeypatch.setattr("trader.bootstrap.MarketDataGateway.fetch_long_quotes", quotes)
    system = BuildProfiledSystem(_config(tmp_path), profiles=profiles)
    primary = system.systems[0]
    assert primary.owns_shared_resources
    codes = primary.long_runtime.codes
    primary.market_data.eligibility.record(
        tuple(
            IssuerEligibilityFact(code, reason, STARTED_AT, f"fixture:{code}", "fixture", "a" * 64)
            for code, reason in zip(
                codes[:2],
                (IssuerEligibilityReason.HISTORICAL_ST, IssuerEligibilityReason.HISTORICAL_DELISTING_WARNING),
                strict=True,
            )
        )
    )
    primary.run_scope.initialize()
    try:
        primary.long_runtime.start()
        primary.scheduler.start()
        primary.scheduler.submit_due()
        assert primary.scheduler.wait_idle(3.0)
        assert primary.long_runtime.wait_idle(3.0)
        assert primary.scheduler.status().preview_error == "production_model_missing"
        assert primary.long_runtime.status().worker.failed_count == 0
        client = system.app.test_client()
        payload = client.get("/api/decisions/long/current").get_json()
        assert payload["status"] == "ready"
        assert len(payload["items"]) == len(primary.long_runtime.codes) > 0
        assert payload["coverage"]["available_quote_count"] == (len(codes) if quote_available else 0)
        first_quote = payload["items"][0]["quote"]
        assert first_quote["status"] == ("live" if quote_available else "missing")
        if quote_available:
            assert first_quote["pct_change"] == 1.75
            assert first_quote["source_time"] == close.isoformat()
        primary.scheduler.submit_due()
        assert primary.long_runtime.wait_idle(3.0)
        assert calls == [(codes, primary.run_scope.preview)]
        assert primary.market_data.eligibility.filter_codes(codes, STARTED_AT) == codes[2:]
        assert primary.tomorrow_index.snapshot(Strategy.LONG).formal is None
        assert not (tmp_path / "data/recommendation").exists()
        assert not (tmp_path / "data/research").exists()
    finally:
        assert primary.scheduler.stop(ShutdownDeadline.start(3.0)).completed
        assert primary.long_runtime.stop(wait=True, deadline=ShutdownDeadline.start(3.0)).completed
