from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta
from math import nan
from zoneinfo import ZoneInfo

import pytest

from trader.domain.market.epochs import (
    CandidateQuoteEpoch,
    DailyFeaturePack,
    DailyFeatureRow,
    MarketEpoch,
    ResearchEpoch,
)
from trader.domain.market.models import Board, LiveQuote, MarketQuote
from trader.domain.market.research import (
    CorporateRiskCategory,
    CorporateRiskFact,
    ResearchAnnouncement,
    ResearchObservation,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")
OBSERVED_AT = datetime(2026, 7, 28, 10, 0, tzinfo=SHANGHAI)
RECEIVED_AT = OBSERVED_AT + timedelta(milliseconds=120)


def _market_quote(code: str = "600001", *, price: float | None = 10.0) -> MarketQuote:
    return MarketQuote(
        code=code,
        name=f"stock-{code}",
        price=price,
        previous_close=9.8,
        open_price=9.9,
        high=10.1,
        low=9.7,
        pct_change=2.04,
        change_5m=0.1,
        speed=0.2,
        volume_ratio=1.2,
        turnover_rate=2.5,
        amount=100_000_000.0,
        amplitude=4.0,
        market_cap=10_000_000_000.0,
        industry="industry",
        source="eastmoney",
        source_time=OBSERVED_AT,
        received_time=RECEIVED_AT,
        data_version=f"quote-{code}",
        board=Board.MAIN,
    )


def _daily_pack(sequence: int = 1) -> DailyFeaturePack:
    return DailyFeaturePack(
        trade_date=date(2026, 7, 28),
        sequence=sequence,
        observed_at=OBSERVED_AT,
        received_at=RECEIVED_AT,
        config_version="runtime-v2",
        rows=(
            DailyFeatureRow(
                code="600001",
                values={"ma20": 9.5, "atr20": 0.4},
                history_sessions=61,
                data_as_of=date(2026, 7, 27),
            ),
        ),
        source_versions={"tencent_qfq": "history-v1"},
    )


def test_daily_feature_pack_is_deeply_immutable_and_hashes_canonical_content() -> None:
    values = {"ma20": 9.5, "atr20": 0.4}
    sources = {"tencent_qfq": "history-v1", "reference": "reference-v1"}
    first = DailyFeaturePack(
        trade_date=date(2026, 7, 28),
        sequence=1,
        observed_at=OBSERVED_AT,
        received_at=RECEIVED_AT,
        config_version="runtime-v2",
        rows=(
            DailyFeatureRow(
                code="600002",
                values={"ma20": 8.0},
                history_sessions=61,
                data_as_of=date(2026, 7, 27),
            ),
            DailyFeatureRow(
                code="600001",
                values=values,
                history_sessions=61,
                data_as_of=date(2026, 7, 27),
            ),
        ),
        source_versions=sources,
    )
    second = DailyFeaturePack(
        trade_date=date(2026, 7, 28),
        sequence=1,
        observed_at=OBSERVED_AT,
        received_at=RECEIVED_AT,
        config_version="runtime-v2",
        rows=tuple(reversed(first.rows)),
        source_versions=dict(reversed(tuple(sources.items()))),
    )

    values["ma20"] = 0.0
    sources["tencent_qfq"] = "changed"

    assert tuple(row.code for row in first.rows) == ("600001", "600002")
    assert first.rows[0].values["ma20"] == 9.5
    assert first.source_versions["tencent_qfq"] == "history-v1"
    assert first.content_hash == second.content_hash
    assert first.version == second.version
    with pytest.raises(TypeError):
        first.rows[0].values["ma20"] = 1.0  # type: ignore[index]


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("observed_at", OBSERVED_AT.replace(tzinfo=None), "timezone-aware"),
        ("observed_at", OBSERVED_AT.astimezone(ZoneInfo("UTC")), "Asia/Shanghai"),
        ("trade_date", date(2026, 7, 27), "trade_date"),
    ),
)
def test_epoch_identity_requires_shanghai_business_time(
    field: str,
    value: date | datetime,
    message: str,
) -> None:
    arguments = {
        "trade_date": date(2026, 7, 28),
        "sequence": 1,
        "observed_at": OBSERVED_AT,
        "received_at": RECEIVED_AT,
        "config_version": "runtime-v2",
        "rows": (),
        "source_versions": {"history": "v1"},
    }
    arguments[field] = value

    with pytest.raises(ValueError, match=message):
        DailyFeaturePack(**arguments)  # type: ignore[arg-type]


def test_daily_features_reject_non_finite_values_and_duplicate_codes() -> None:
    with pytest.raises(ValueError, match="finite"):
        DailyFeatureRow(
            code="600001",
            values={"ma20": nan},
            history_sessions=20,
            data_as_of=date(2026, 7, 27),
        )

    row = DailyFeatureRow(
        code="600001",
        values={"ma20": 9.5},
        history_sessions=20,
        data_as_of=date(2026, 7, 27),
    )
    with pytest.raises(ValueError, match="unique"):
        DailyFeaturePack(
            trade_date=date(2026, 7, 28),
            sequence=1,
            observed_at=OBSERVED_AT,
            received_at=RECEIVED_AT,
            config_version="runtime-v2",
            rows=(row, row),
            source_versions={"history": "v1"},
        )


def test_market_and_candidate_epochs_bind_parent_versions_and_reject_invalid_quotes() -> None:
    pack = _daily_pack()
    market = MarketEpoch(
        trade_date=pack.trade_date,
        sequence=1,
        observed_at=OBSERVED_AT,
        received_at=RECEIVED_AT,
        config_version="runtime-v2",
        daily_feature_pack_version=pack.version,
        quotes=(_market_quote(),),
        source_versions={"eastmoney": "market-v1"},
        degraded_reasons=("sina_timeout",),
    )
    candidate = CandidateQuoteEpoch(
        trade_date=market.trade_date,
        sequence=1,
        observed_at=OBSERVED_AT,
        received_at=RECEIVED_AT,
        config_version="runtime-v2",
        market_epoch_version=market.version,
        quotes=(
            LiveQuote(
                code="600001",
                price=10.01,
                pct_change=2.14,
                source="tencent",
                source_time=OBSERVED_AT,
                received_time=RECEIVED_AT,
                data_version="candidate-v1",
            ),
        ),
        source_versions={"tencent": "candidate-v1"},
    )

    assert market.daily_feature_pack_version == pack.version
    assert candidate.market_epoch_version == market.version
    assert len(market.content_hash) == 64
    assert len(candidate.content_hash) == 64
    assert market.degraded_reasons == ("sina_timeout",)

    with pytest.raises(ValueError, match="finite"):
        MarketEpoch(
            trade_date=pack.trade_date,
            sequence=2,
            observed_at=OBSERVED_AT,
            received_at=RECEIVED_AT,
            config_version="runtime-v2",
            daily_feature_pack_version=pack.version,
            quotes=(_market_quote(price=nan),),
            source_versions={"eastmoney": "market-v2"},
        )


def test_research_epoch_is_immutable_and_rejects_future_evidence() -> None:
    observations = {
        "600001": ResearchObservation(
            announcements=(
                ResearchAnnouncement(
                    title="公告",
                    published_at=OBSERVED_AT - timedelta(hours=1),
                    announcement_id="notice-1",
                ),
            ),
            announcements_available=True,
        )
    }
    epoch = ResearchEpoch(
        trade_date=date(2026, 7, 28),
        sequence=1,
        observed_at=OBSERVED_AT,
        received_at=RECEIVED_AT,
        config_version="runtime-v2",
        observations=observations,
        source_versions={"issuer": "research-v1"},
    )
    observations.clear()

    assert tuple(epoch.observations) == ("600001",)
    with pytest.raises(TypeError):
        epoch.observations["600002"] = ResearchObservation()  # type: ignore[index]

    with pytest.raises(ValueError, match="future"):
        ResearchEpoch(
            trade_date=date(2026, 7, 28),
            sequence=2,
            observed_at=OBSERVED_AT,
            received_at=RECEIVED_AT,
            config_version="runtime-v2",
            observations={
                "600001": ResearchObservation(
                    announcements=(
                        ResearchAnnouncement(
                            title="未来公告",
                            published_at=OBSERVED_AT + timedelta(seconds=1),
                            announcement_id="notice-future",
                        ),
                    ),
                    announcements_available=True,
                )
            },
            source_versions={"issuer": "research-v2"},
        )


def test_epoch_rejects_missing_source_identity_and_empty_full_market_payloads() -> None:
    with pytest.raises(ValueError, match="source_versions"):
        DailyFeaturePack(
            trade_date=date(2026, 7, 28),
            sequence=1,
            observed_at=OBSERVED_AT,
            received_at=RECEIVED_AT,
            config_version="runtime-v2",
            rows=(
                DailyFeatureRow(
                    code="600001",
                    values={"ma20": 9.5},
                    history_sessions=61,
                    data_as_of=date(2026, 7, 27),
                ),
            ),
            source_versions={},
        )

    pack = _daily_pack()
    with pytest.raises(ValueError, match="market quotes must not be empty"):
        MarketEpoch(
            trade_date=pack.trade_date,
            sequence=1,
            observed_at=OBSERVED_AT,
            received_at=RECEIVED_AT,
            config_version="runtime-v2",
            daily_feature_pack_version=pack.version,
            quotes=(),
            source_versions={"eastmoney": "market-v1"},
        )


def test_epoch_rejects_invalid_quote_and_research_event_time_ordering() -> None:
    pack = _daily_pack()
    with pytest.raises(ValueError, match="cannot precede source_time"):
        MarketEpoch(
            trade_date=pack.trade_date,
            sequence=1,
            observed_at=OBSERVED_AT,
            received_at=RECEIVED_AT,
            config_version="runtime-v2",
            daily_feature_pack_version=pack.version,
            quotes=(
                replace(
                    _market_quote(),
                    received_time=OBSERVED_AT - timedelta(milliseconds=1),
                ),
            ),
            source_versions={"eastmoney": "market-v1"},
        )

    with pytest.raises(ValueError, match="future risk resolutions"):
        ResearchEpoch(
            trade_date=date(2026, 7, 28),
            sequence=1,
            observed_at=OBSERVED_AT,
            received_at=RECEIVED_AT,
            config_version="runtime-v2",
            observations={
                "600001": ResearchObservation(
                    corporate_risk_facts=(
                        CorporateRiskFact(
                            category=CorporateRiskCategory.OFFICIAL_INVESTIGATION,
                            announced_at=OBSERVED_AT - timedelta(days=1),
                            resolved_at=OBSERVED_AT + timedelta(seconds=1),
                            evidence_id="risk-1",
                            source="regulator_disclosure",
                        ),
                    ),
                    corporate_risk_history_complete=True,
                    corporate_risk_registry_version="risk-v1",
                )
            },
            source_versions={"regulator": "research-v1"},
        )
