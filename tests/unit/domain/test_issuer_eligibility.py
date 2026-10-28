from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from trader.recommendation.domain.market.eligibility import (
    EligibilityFactsFromQuote,
    IssuerEligibilityFact,
    IssuerEligibilityReason,
    IssuerEligibilityState,
    ResolveIssuerEligibility,
)
from trader.recommendation.domain.market.models import MarketQuote

SHANGHAI = ZoneInfo("Asia/Shanghai")
OBSERVED_AT = datetime(2026, 9, 1, 10, 0, tzinfo=SHANGHAI)


def _fact(*, effective_at: datetime = OBSERVED_AT) -> IssuerEligibilityFact:
    return IssuerEligibilityFact(
        code="600001",
        reason=IssuerEligibilityReason.HISTORICAL_ST,
        effective_at=effective_at,
        evidence_id="quote:600001:fixture",
        source="eastmoney_market",
        evidence_hash="a" * 64,
    )


def _quote(**overrides: object) -> MarketQuote:
    values: dict[str, object] = {
        "code": "600001",
        "name": "测试股份",
        "price": 10.0,
        "previous_close": 9.8,
        "open_price": 9.9,
        "high": 10.1,
        "low": 9.7,
        "pct_change": 2.04,
        "change_5m": 0.1,
        "speed": 0.1,
        "volume_ratio": 1.0,
        "turnover_rate": 2.0,
        "amount": 100_000_000.0,
        "amplitude": 4.0,
        "market_cap": 10_000_000_000.0,
        "industry": "测试",
        "source": "eastmoney",
        "source_time": OBSERVED_AT,
        "received_time": OBSERVED_AT,
        "data_version": "quote-fixture",
    }
    values.update(overrides)
    return MarketQuote(**values)  # type: ignore[arg-type]


def test_permanent_fact_only_excludes_from_its_effective_time() -> None:
    fact = _fact()

    before = ResolveIssuerEligibility((fact,), "600001", OBSERVED_AT - timedelta(microseconds=1))
    at_boundary = ResolveIssuerEligibility((fact,), "600001", OBSERVED_AT)

    assert before.state is IssuerEligibilityState.ELIGIBLE_UNVERIFIED
    assert at_boundary.state is IssuerEligibilityState.PERMANENTLY_EXCLUDED
    assert at_boundary.reason is IssuerEligibilityReason.HISTORICAL_ST


def test_quote_status_creates_stable_permanent_facts_without_using_free_text_news() -> None:
    facts = EligibilityFactsFromQuote(
        _quote(name="*ST测试", is_st=True, is_blacklisted=True),
        observed_at=OBSERVED_AT,
    )

    assert {fact.reason for fact in facts} == {
        IssuerEligibilityReason.HISTORICAL_ST,
        IssuerEligibilityReason.MANUAL_PERMANENT_BLACKLIST,
    }
    assert all(fact.effective_at == OBSERVED_AT for fact in facts)
    assert all(len(fact.evidence_hash) == 64 for fact in facts)


def test_quote_fact_identity_accepts_untrusted_supplier_version() -> None:
    facts = EligibilityFactsFromQuote(
        _quote(is_st=True, source="来源 with spaces", data_version="版本 " * 200),
        observed_at=OBSERVED_AT,
    )
    freeze = ResolveIssuerEligibility(facts, "600001", OBSERVED_AT)
    assert freeze.state is IssuerEligibilityState.PERMANENTLY_EXCLUDED
    assert len(facts[0].evidence_id) < 80


def test_old_confirmed_company_risk_does_not_create_permanent_exclusion() -> None:
    fact = IssuerEligibilityFact(
        code="600001",
        reason=IssuerEligibilityReason.CONFIRMED_FINANCIAL_FRAUD,
        effective_at=OBSERVED_AT - timedelta(days=400),
        evidence_id="announcement:old-fraud",
        source="issuer_disclosure",
        evidence_hash="c" * 64,
    )
    assert ResolveIssuerEligibility((fact,), "600001", OBSERVED_AT).state is IssuerEligibilityState.ELIGIBLE_UNVERIFIED


def test_retired_historical_loss_fact_is_auditable_but_not_a_permanent_exclusion() -> None:
    legacy = IssuerEligibilityFact(
        code="600001",
        reason=IssuerEligibilityReason.HISTORICAL_AUDITED_LOSS,
        effective_at=OBSERVED_AT - timedelta(days=400),
        evidence_id="financial:600001:2024-12-31",
        source="eastmoney_financial",
        evidence_hash="b" * 64,
    )

    freeze = ResolveIssuerEligibility((legacy,), "600001", OBSERVED_AT)

    assert freeze.state is IssuerEligibilityState.ELIGIBLE_UNVERIFIED
    assert freeze.reason is None
