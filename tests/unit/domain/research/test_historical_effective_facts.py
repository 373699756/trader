from datetime import date

from trader.domain.research.historical_effective_facts import (
    HistoricalEffectiveFactsProbe,
    build_historical_effective_facts_audit,
)


def test_baostock_alone_cannot_authorize_historical_effective_facts() -> None:
    report = build_historical_effective_facts_audit(
        (
            HistoricalEffectiveFactsProbe(
                source="baostock",
                earliest_available=date(2018, 1, 1),
                industry_effective_at=False,
                eligibility_effective_at=False,
                hard_filter_effective_at=False,
                risk_facts_effective_at=False,
            ),
        )
    )

    assert report.status == "historical_data_insufficient"
    assert report.failure_reasons == (
        "historical_eligibility_effective_at_unavailable",
        "historical_hard_filter_effective_at_unavailable",
        "historical_industry_effective_at_unavailable",
        "historical_risk_facts_effective_at_unavailable",
    )
    assert report.production_authority is False
    assert report.v3_training_authority is False
    assert report.point_in_time_parity is False
