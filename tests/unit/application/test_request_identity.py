from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal

import pytest

from tests.unit.application.test_input_runtime import _policy
from tests.unit.application.test_tomorrow_projection import EVALUATED_AT, _native_input, _verified_feature
from trader.infra.serialization.canonical import canonical_json_text
from trader.recommendation.application.pipeline.final_selection.decision_projection import build_scored_local
from trader.recommendation.application.pipeline.quality_check.input_quality_service import ScoredInputQuality
from trader.recommendation.application.ports import scoring
from trader.recommendation.application.ports.scoring import D25NativeInput, TomorrowNativeInput
from trader.recommendation.application.request_identity import request_fingerprint
from trader.recommendation.domain.evidence.review import RiskFact
from trader.recommendation.domain.market.models import (
    Board,
    BoardPopulation,
    CrossSectionStats,
    Evidence,
    ModelIndustryReference,
)


def _complete_feature(factory):
    feature = _verified_feature(factory("600000", EVALUATED_AT))
    return replace(
        feature,
        quote=replace(
            feature.quote,
            listing_date=date(2000, 1, 1),
            listing_age_sessions=5000,
            is_relisted_first_session=False,
            is_delisting_period_first_session=False,
            has_price_limit=True,
            exchange_limit_pct=10.0,
            strategy_hot_cap_pct=8.0,
            rule_version="rule-fixture",
            rule_effective_date=date(2020, 1, 1),
        ),
        evidence=(
            Evidence("evidence-fixture", "official", "事实", "exchange", EVALUATED_AT, EVALUATED_AT, "ev-fixture"),
        ),
        external_risk_facts=(
            RiskFact(
                "risk-fixture", "risk", "warning", 0.0, "exchange", EVALUATED_AT, evidence_ids=("evidence-fixture",)
            ),
        ),
        normalization={"trend_score": CrossSectionStats(0.0, 100.0, 100, 0, 0.025, 0.975, "population-fixture")},
        board_population=BoardPopulation(
            EVALUATED_AT.date().isoformat(),
            "final_review",
            Board.MAIN,
            "data-fixture",
            "schema-fixture",
            "population-fixture",
            100,
            0,
            50.0,
            80.0,
        ),
        model_industry=ModelIndustryReference(
            "C", "证监会行业分类", EVALUATED_AT.date(), "baostock", "industry-fixture"
        ),
        missing_fields=("optional-fixture",),
        missing_reasons={"optional-fixture": "unavailable"},
    )


def _previous_feature_fingerprint(material):
    # The pre-change dataclass wire shape matches the established canonical codec
    # for these native values. Keep this independent of the new explicit projector.
    return hashlib.sha256(canonical_json_text(material, ascii_only=False).encode("utf-8")).hexdigest()


@pytest.mark.parametrize("native_type", (TomorrowNativeInput, D25NativeInput))
def test_explicit_identity_preserves_native_and_published_decision_versions(
    application_feature_factory,
    monkeypatch,
    native_type,
):
    feature = _complete_feature(application_feature_factory)
    assert request_fingerprint({"feature": feature}) == _previous_feature_fingerprint({"feature": feature})
    native = _native_input((feature,), native_type)
    projection = build_scored_local(native, _policy(), sequence=1)
    with monkeypatch.context() as previous:
        previous.setattr(scoring, "request_fingerprint", _previous_feature_fingerprint)
        baseline_native = _native_input((feature,), native_type)
        baseline_projection = build_scored_local(baseline_native, _policy(), sequence=1)
    assert native.input_version == baseline_native.input_version
    assert projection.local == baseline_projection.local
    assert projection.input_quality == baseline_projection.input_quality
    changed = replace(feature, values={**feature.values, "trend_score": 69.0})
    assert _native_input((changed,), native_type).input_version != native.input_version


def test_request_identity_rejects_unregistered_dataclasses():
    @dataclass(frozen=True)
    class UnknownInput:
        secret: str

    with pytest.raises(TypeError, match="unsupported request identity value"):
        request_fingerprint({"input": UnknownInput("private")})


def test_request_identity_keeps_code_set_decimal_and_timezone_rules():
    assert request_fingerprint(
        {"codes": ("600000", "300001", "600000"), "amount": Decimal("1.00")}
    ) == request_fingerprint({"amount": "1.00", "codes": ["300001", "600000"]})
    with pytest.raises(ValueError, match="finite"):
        request_fingerprint({"amount": Decimal("NaN")})
    with pytest.raises(ValueError, match="timezone-aware"):
        request_fingerprint({"observed_at": datetime(2026, 1, 1)})


def _quality():
    return ScoredInputQuality("ready", 1, 1, 1, 0, 0, 1, 1, 1, 20, 1.0, 1.0, 1.0)


@pytest.mark.parametrize(
    "changes",
    (
        {"population_count": -1},
        {"candidate_count": -1},
        {"candidate_feature_count": -1},
        {"population_rejected_count": -1},
        {"candidate_rejected_count": -1},
        {"candidate_scored_count": -1},
        {"security_master_covered_count": -1},
        {"history_covered_count": -1},
        {"data_pending_count": -1},
        {"refresh_pending_count": -1},
        {"candidate_feature_coverage_ratio": float("nan")},
        {"security_master_coverage_ratio": float("inf")},
        {"history_coverage_ratio": 1.1},
        {"population_filter_reason_counts": {"reason": -1}},
        {"candidate_filter_reason_counts": {"": 1}},
        {"candidate_transient_reason_counts": {"reason": -1}},
        {"candidate_optional_reason_counts": {"": 0}},
    ),
)
def test_explicit_quality_validation_preserves_invalid_input_rejection(changes):
    with pytest.raises(ValueError):
        replace(_quality(), **changes)


def test_quality_reason_counts_are_copied_sorted_and_immutable():
    reasons = {"z": 1, "a": 2}
    quality = replace(
        _quality(),
        population_filter_reason_counts=reasons,
        candidate_filter_reason_counts=reasons,
        candidate_transient_reason_counts=reasons,
        candidate_optional_reason_counts=reasons,
    )
    reasons["a"] = 9
    for counts in (
        quality.population_filter_reason_counts,
        quality.candidate_filter_reason_counts,
        quality.candidate_transient_reason_counts,
        quality.candidate_optional_reason_counts,
    ):
        assert tuple(counts.items()) == (("a", 2), ("z", 1))
        with pytest.raises(TypeError):
            counts["a"] = 3
