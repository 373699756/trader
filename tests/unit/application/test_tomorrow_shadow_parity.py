from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from trader.application.ports.tomorrow import TomorrowNativeInput
from trader.application.tomorrow_shadow_projection import project_tomorrow_input
from trader.application.tomorrow_shadow_runtime import _same_hard_filter_reasons
from trader.bootstrap import _recommendation_policy
from trader.domain.market.models import FeatureSnapshot
from trader.infra.settings import load_strategy_settings

SHANGHAI = ZoneInfo("Asia/Shanghai")
TRADE_DATE = date(2026, 7, 29)
EVALUATED_AT = datetime(2026, 7, 29, 13, 50, tzinfo=SHANGHAI)
PROJECT_ROOT = Path(__file__).resolve().parents[3]


def test_native_projection_uses_input_hash_audit_ids_and_batch_ready_watermark(
    application_feature_factory,
) -> None:
    policy = _recommendation_policy(load_strategy_settings(PROJECT_ROOT / "config" / "v2" / "strategy.json"))
    quote_at = EVALUATED_AT - timedelta(seconds=20)
    features = tuple(
        _verified_feature(application_feature_factory(code, quote_at))
        for code in ("600001", "600002", "300001", "300002", "688001", "688002")
    )
    native_input = _native_input(features)

    projection = project_tomorrow_input(native_input, policy, decision_sequence=4)

    input_hash = native_input.input_version.removeprefix("native-input:")
    assert projection.received_at == native_input.evaluated_at
    assert projection.local.market_epoch_version == f"native-market:{input_hash}"
    assert projection.local.candidate_epoch_version == f"native-candidate:{input_hash}"


def test_hard_filter_comparison_removes_only_v1_history_warming_diagnostic() -> None:
    native_reasons = {"missing_liquidity_history": 6, "st_or_delisting": 1}

    assert _same_hard_filter_reasons(
        {
            "history_warming": 6,
            "missing_liquidity_history": 6,
            "st_or_delisting": 1,
        },
        native_reasons,
    )
    assert not _same_hard_filter_reasons(
        {
            "history_warming": 6,
            "missing_liquidity_history": 5,
            "st_or_delisting": 1,
        },
        native_reasons,
    )
    assert not _same_hard_filter_reasons(
        {
            "history_warming": 6,
            "missing_liquidity_history": 6,
            "st_or_delisting": 1,
            "invalid_price": 1,
        },
        native_reasons,
    )
    assert not _same_hard_filter_reasons(
        {
            "missing_liquidity_history": 6,
            "st_or_delisting": 1,
        },
        {**native_reasons, "history_warming": 6},
    )


def _native_input(features: tuple[FeatureSnapshot, ...]) -> TomorrowNativeInput:
    return TomorrowNativeInput(
        trade_date=TRADE_DATE,
        phase="afternoon",
        data_version="candidate-data:shared",
        config_version="runtime:test",
        evaluated_at=EVALUATED_AT,
        market_features=features,
        requested_codes=tuple(feature.quote.code for feature in features),
        candidate_features=features,
        preselect_max_age_seconds=30.0,
        score_max_age_seconds=30.0,
        candidate_pool_size=120,
    )


def _verified_feature(feature: FeatureSnapshot) -> FeatureSnapshot:
    return replace(
        feature,
        quote=replace(
            feature.quote,
            cross_source_verified=True,
            cross_source_deviation_pct=0.1,
        ),
    )
