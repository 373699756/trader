from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from trader.application.ports.tomorrow import TomorrowNativeInput
from trader.application.tomorrow_shadow_projection import project_tomorrow_input
from trader.bootstrap import _recommendation_policy
from trader.domain.market.models import Evidence, FeatureSnapshot
from trader.domain.review.models import RiskFact
from trader.infra.settings import load_strategy_settings

SHANGHAI = ZoneInfo("Asia/Shanghai")
TRADE_DATE = date(2026, 7, 28)
OBSERVED_AT = datetime(2026, 7, 28, 14, 40, tzinfo=SHANGHAI)
PROJECT_ROOT = Path(__file__).resolve().parents[3]


def test_native_input_normalizes_utc_feature_times_before_risk_projection(
    application_feature_factory,
) -> None:
    policy = _recommendation_policy(load_strategy_settings(PROJECT_ROOT / "config" / "v2" / "strategy.json"))
    features = tuple(
        _risk_feature(application_feature_factory(code, OBSERVED_AT))
        for code in ("600001", "600002", "300001", "300002", "688001", "688002")
    )
    shanghai_input = _native_input(features)
    utc_input = _native_input(tuple(_as_utc(feature) for feature in features))

    shanghai_projection = project_tomorrow_input(shanghai_input, policy, decision_sequence=4)
    utc_projection = project_tomorrow_input(utc_input, policy, decision_sequence=4)

    assert utc_input.input_version == shanghai_input.input_version
    assert utc_projection.local.version == shanghai_projection.local.version
    assert all(
        getattr(feature.observed_at.tzinfo, "key", None) == "Asia/Shanghai" for feature in utc_input.candidate_features
    )
    assert all(
        getattr(item.published_at.tzinfo, "key", None) == "Asia/Shanghai"
        and getattr(item.received_at.tzinfo, "key", None) == "Asia/Shanghai"
        for feature in utc_input.candidate_features
        for item in feature.evidence
    )
    risk_facts = tuple(fact for entry in utc_projection.local.entries for fact in entry.local_risk_facts)
    assert risk_facts
    assert all(getattr(fact.observed_at.tzinfo, "key", None) == "Asia/Shanghai" for fact in risk_facts)


def test_native_input_rejects_future_risk_and_evidence_times(
    application_feature_factory,
) -> None:
    features = tuple(
        application_feature_factory(code, OBSERVED_AT)
        for code in ("600001", "600002", "300001", "300002", "688001", "688002")
    )
    native_input = _native_input(features)
    future_fact = RiskFact(
        risk_fact_id="future-risk",
        risk_code="high_volatility",
        severity="low",
        penalty=3.0,
        source="fixture",
        observed_at=(OBSERVED_AT + timedelta(seconds=1)).astimezone(timezone.utc),
    )

    with pytest.raises(ValueError, match="future risk facts"):
        replace(
            native_input,
            candidate_features=(
                replace(native_input.candidate_features[0], external_risk_facts=(future_fact,)),
                *native_input.candidate_features[1:],
            ),
        )

    future_evidence = Evidence(
        evidence_id="future-evidence",
        evidence_type="structured_point_in_time",
        title="Future evidence",
        source="fixture",
        published_at=(OBSERVED_AT + timedelta(seconds=1)).astimezone(timezone.utc),
    )
    with pytest.raises(ValueError, match="future evidence"):
        replace(
            native_input,
            candidate_features=(
                replace(native_input.candidate_features[0], evidence=(future_evidence,)),
                *native_input.candidate_features[1:],
            ),
        )


def _native_input(features: tuple[FeatureSnapshot, ...]) -> TomorrowNativeInput:
    return TomorrowNativeInput(
        trade_date=TRADE_DATE,
        phase="afternoon",
        data_version="candidate-data:shared",
        config_version="runtime:test",
        evaluated_at=OBSERVED_AT,
        market_features=features,
        requested_codes=tuple(feature.quote.code for feature in features),
        candidate_features=features,
        preselect_max_age_seconds=30.0,
        score_max_age_seconds=30.0,
        candidate_pool_size=120,
    )


def _risk_feature(feature: FeatureSnapshot) -> FeatureSnapshot:
    return replace(
        feature,
        values={**feature.values, "volatility_20d": 5.0},
        evidence=(
            Evidence(
                evidence_id=f"evidence:{feature.quote.code}",
                evidence_type="structured_point_in_time",
                title="UTC normalization fixture",
                source="fixture",
                published_at=feature.observed_at,
                received_at=feature.observed_at,
            ),
        ),
        external_risk_facts=(
            RiskFact(
                risk_fact_id=f"external:{feature.quote.code}",
                risk_code="high_volatility",
                severity="low",
                penalty=3.0,
                source="fixture",
                observed_at=feature.observed_at,
            ),
        ),
    )


def _as_utc(feature: FeatureSnapshot) -> FeatureSnapshot:
    return replace(
        feature,
        quote=replace(
            feature.quote,
            source_time=feature.quote.source_time.astimezone(timezone.utc),
            received_time=feature.quote.received_time.astimezone(timezone.utc),
        ),
        observed_at=feature.observed_at.astimezone(timezone.utc),
        evidence=tuple(
            replace(
                item,
                published_at=item.published_at.astimezone(timezone.utc),
                received_at=item.received_at.astimezone(timezone.utc) if item.received_at is not None else None,
            )
            for item in feature.evidence
        ),
        external_risk_facts=tuple(
            replace(fact, observed_at=fact.observed_at.astimezone(timezone.utc)) for fact in feature.external_risk_facts
        ),
    )
