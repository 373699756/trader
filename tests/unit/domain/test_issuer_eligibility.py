from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from trader.domain.market.eligibility import (
    IssuerEligibilityFact,
    IssuerEligibilityReason,
    IssuerEligibilityState,
    eligibility_facts_from_quote,
    eligibility_facts_from_research,
    resolve_issuer_eligibility,
)
from trader.domain.market.models import MarketQuote
from trader.domain.market.research import (
    CorporateRiskCategory,
    CorporateRiskFact,
    FinancialReport,
    ResearchObservation,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")
OBSERVED_AT = datetime(2026, 9, 1, 10, 0, tzinfo=SHANGHAI)


def _fact(*, effective_at: datetime = OBSERVED_AT) -> IssuerEligibilityFact:
    return IssuerEligibilityFact(
        code="600001",
        reason=IssuerEligibilityReason.HISTORICAL_ST,
        effective_at=effective_at,
        evidence_id="quote:600001:v1",
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
        "data_version": "quote-v1",
    }
    values.update(overrides)
    return MarketQuote(**values)  # type: ignore[arg-type]


def test_permanent_fact_only_excludes_from_its_effective_time() -> None:
    fact = _fact()

    before = resolve_issuer_eligibility((fact,), "600001", OBSERVED_AT - timedelta(microseconds=1))
    at_boundary = resolve_issuer_eligibility((fact,), "600001", OBSERVED_AT)

    assert before.state is IssuerEligibilityState.ELIGIBLE_UNVERIFIED
    assert at_boundary.state is IssuerEligibilityState.PERMANENTLY_EXCLUDED
    assert at_boundary.reason is IssuerEligibilityReason.HISTORICAL_ST


def test_quote_status_creates_stable_permanent_facts_without_using_free_text_news() -> None:
    facts = eligibility_facts_from_quote(
        _quote(name="*ST测试", is_st=True, is_blacklisted=True),
        observed_at=OBSERVED_AT,
    )

    assert {fact.reason for fact in facts} == {
        IssuerEligibilityReason.HISTORICAL_ST,
        IssuerEligibilityReason.MANUAL_PERMANENT_BLACKLIST,
    }
    assert all(fact.effective_at == OBSERVED_AT for fact in facts)
    assert all(len(fact.evidence_hash) == 64 for fact in facts)


def test_research_creates_loss_and_confirmed_permanent_risk_facts_only() -> None:
    annual_loss = FinancialReport(
        report_date=date(2024, 12, 31),
        published_at=OBSERVED_AT - timedelta(days=400),
        parent_net_profit=-1.0,
        core_net_profit=2.0,
    )
    quarterly_loss = FinancialReport(
        report_date=date(2025, 9, 30),
        published_at=OBSERVED_AT - timedelta(days=300),
        parent_net_profit=-5.0,
        core_net_profit=-6.0,
    )
    fraud = CorporateRiskFact(
        category=CorporateRiskCategory.FINANCIAL_FRAUD,
        announced_at=OBSERVED_AT - timedelta(days=200),
        evidence_id="announcement:fraud",
        source="issuer_disclosure",
    )
    investigation = CorporateRiskFact(
        category=CorporateRiskCategory.OFFICIAL_INVESTIGATION,
        announced_at=OBSERVED_AT - timedelta(days=100),
        evidence_id="announcement:investigation",
        source="issuer_disclosure",
    )
    observation = ResearchObservation(
        financial=quarterly_loss,
        financial_history=(annual_loss, quarterly_loss),
        corporate_risk_facts=(fraud, investigation),
    )

    facts = eligibility_facts_from_research("600001", observation)

    assert {fact.reason for fact in facts} == {
        IssuerEligibilityReason.HISTORICAL_AUDITED_LOSS,
        IssuerEligibilityReason.CONFIRMED_FINANCIAL_FRAUD,
    }
    loss = next(fact for fact in facts if fact.reason is IssuerEligibilityReason.HISTORICAL_AUDITED_LOSS)
    assert loss.effective_at == annual_loss.published_at
