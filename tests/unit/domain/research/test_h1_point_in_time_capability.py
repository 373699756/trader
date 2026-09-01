from datetime import date

import pytest

from trader.domain.research.h1_point_in_time import (
    H1CapabilityAuditReport,
    H1CapabilityProbe,
    build_h1_capability_audit,
)


def _probe(
    source: str,
    *,
    earliest: date | None,
    today: bool,
    afternoon: bool,
    adjustment: str,
    effective_state: bool,
    rows: int,
) -> H1CapabilityProbe:
    return H1CapabilityProbe(
        source=source,
        earliest_available=earliest,
        supports_today_1120=today,
        supports_1450=afternoon,
        adjustment_semantics=adjustment,
        security_state_effective_at=effective_state,
        page_size=rows,
        estimated_requests=3,
        estimated_bytes=1024,
        estimated_seconds=0.5,
    )


def test_capability_audit_fails_each_strategy_closed_when_free_sources_lack_historical_anchors() -> None:
    report = build_h1_capability_audit(
        (
            _probe(
                "tencent_qfq_daily",
                earliest=date(2023, 1, 10),
                today=False,
                afternoon=False,
                adjustment="qfq",
                effective_state=False,
                rows=640,
            ),
            _probe(
                "eastmoney_historical_minute",
                earliest=None,
                today=False,
                afternoon=False,
                adjustment="unsupported",
                effective_state=False,
                rows=0,
            ),
        )
    )

    assert isinstance(report, H1CapabilityAuditReport)
    assert tuple(item.strategy for item in report.strategies) == ("today", "tomorrow", "d25")
    assert {item.state for item in report.strategies} == {"historical_data_insufficient"}
    assert "qfq_history_below_1000_sessions" in report.strategies[0].failure_reasons
    assert "historical_1120_anchor_unavailable" in report.strategies[0].failure_reasons
    assert "historical_1450_anchor_unavailable" in report.strategies[1].failure_reasons
    assert all(not item.terminal_holdout_opened for item in report.strategies)
    assert report.production_authority is False


def test_capability_audit_requires_independent_qfq_anchor_and_effective_state_evidence() -> None:
    qfq = _probe(
        "qfq_archive",
        earliest=date(2019, 1, 1),
        today=False,
        afternoon=False,
        adjustment="qfq",
        effective_state=False,
        rows=1600,
    )
    anchor = _probe(
        "minute_archive",
        earliest=date(2019, 1, 1),
        today=True,
        afternoon=True,
        adjustment="unsupported",
        effective_state=False,
        rows=1600,
    )
    report = build_h1_capability_audit((qfq, anchor))

    assert {item.state for item in report.strategies} == {"historical_data_insufficient"}
    assert all("effective_security_state_unavailable" in item.failure_reasons for item in report.strategies)
    with pytest.raises(ValueError, match="source identities"):
        build_h1_capability_audit((qfq, qfq))
