from __future__ import annotations

from tests.component.market_data_test_support import (
    AFTERNOON,
    LONG_POLICY,
    MARKET_REGIME_POLICY,
    NEWS_POLICY,
    NOW,
    TAIL_POLICY,
    ConfigurationError,
    CountingHistoryClient,
    DailyBar,
    FeatureBuilder,
    HistoryAdjustmentError,
    Path,
    PriceAdjustment,
    StaticGateway,
    _history_bars,
    _quote,
    _service,
    _tail_minute_bars,
    json,
    load_strategy_settings,
    pytest,
    replace,
)


def test_feature_builder_does_not_compute_limit_proximity_when_limit_is_inapplicable() -> None:
    quote = replace(
        _quote(),
        has_price_limit=False,
        exchange_limit_pct=None,
        listing_age_sessions=1,
    )

    feature = FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY).build((quote,), {}, NOW)[0]

    assert feature.values["limit_proximity"] is None
    assert feature.values["limit_distance_safety"] is None


def test_feature_builder_marks_history_missing_and_builds_cross_section() -> None:
    quote = _quote()
    bars = tuple(
        DailyBar(
            trade_date=f"2026-06-{index:02d}",
            open_price=10 + index / 100,
            close=10 + index / 100,
            high=10.2 + index / 100,
            low=9.8 + index / 100,
            volume=1_000_000,
            amount=100_000_000 + index,
            pct_change=0.1,
            adjustment=PriceAdjustment.QFQ,
            source="fixture",
        )
        for index in range(1, 61)
    )

    with_history, without_history = FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY).build(
        (quote, _quote(code="600002", industry="银行")),
        {"600001": bars},
        NOW,
    )

    assert with_history.history_days == 60
    assert with_history.optional_value("return_20d") is not None
    assert without_history.history_days == 0
    assert without_history.optional_value("return_20d") is None
    assert "return_20d" in without_history.missing_fields


def test_targeted_feature_build_preserves_full_market_cross_section() -> None:
    builder = FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY)
    low = replace(_quote(code="600001"), speed=0.1)
    middle = replace(_quote(code="600002"), speed=0.2)
    high = replace(_quote(code="600003"), speed=0.3)
    market = builder.build((low, middle, high), {}, NOW)
    reference = {item.quote.code: item.values for item in market}

    targeted = builder.build((high,), {}, NOW, cross_section_reference=reference)

    assert market[-1].values["speed_percentile"] == 100.0
    assert targeted[0].values["speed_percentile"] == 100.0


def test_feature_builder_partitions_cross_sections_and_excludes_missing_breadth() -> None:
    builder = FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY)
    quotes = (
        replace(_quote(code="600001"), speed=0.1, pct_change=1.0, data_version="v1"),
        replace(_quote(code="600002"), speed=0.2, pct_change=-1.0, data_version="v1"),
        replace(_quote(code="600003"), speed=0.1, pct_change=None, data_version="v2"),
        replace(_quote(code="600004"), speed=0.2, pct_change=2.0, data_version="v2"),
    )

    features = builder.build(quotes, {}, NOW)

    assert [item.values["speed_percentile"] for item in features] == [0.0, 100.0, 0.0, 100.0]
    assert features[0].values["market_breadth"] == 50.0
    assert features[2].values["market_breadth"] == 100.0
    assert features[0].market_regime == "neutral"
    assert features[2].market_regime == "risk_on"
    assert features[0].normalization["speed_percentile"].sample_size == 2
    assert features[2].normalization["market_breadth"].missing_count == 1
    assert features[2].values["limit_proximity"] is None
    assert "pct_change" in features[2].missing_fields
    assert "limit_proximity" in features[2].missing_fields


def test_market_service_loads_history_before_cold_start_candidate_cross_section() -> None:
    history = CountingHistoryClient(_history_bars())
    service = _service(
        StaticGateway((_quote(), _quote(code="600002"))),
        history,
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY),
        history_workers=2,
    )

    features = service.fetch_market_features(NOW)

    assert sorted(history.calls) == ["600001", "600002"]
    assert all(item.history_days == 60 for item in features)
    assert service.health()["history_coverage_ratio"] == 1.0
    assert service.health()["history_universe_rows"] == 2


def test_feature_builder_rejects_unadjusted_history_for_qfq_features() -> None:
    raw_bars = tuple(replace(bar, adjustment=PriceAdjustment.RAW, source="tushare") for bar in _history_bars())

    with pytest.raises(HistoryAdjustmentError, match="requires qfq"):
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY).build(
            (_quote(),),
            {"600001": raw_bars},
            NOW,
        )


def test_strategy_factor_registry_is_complete_and_required() -> None:
    path = Path(__file__).parents[2] / "config" / "v2" / "strategy.json"
    settings = load_strategy_settings(path)

    assert settings.factor_registry["speed_percentile"].factor_id == "speed_percentile"
    assert settings.strategy_version.startswith("strategy_sha256_")


def test_strategy_loader_rejects_missing_factor_registration(tmp_path) -> None:
    source = Path(__file__).parents[2] / "config" / "v2" / "strategy.json"
    raw = json.loads(source.read_text(encoding="utf-8"))
    del raw["factor_registry"]["speed_percentile"]
    target = tmp_path / "strategy.json"
    target.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ConfigurationError, match="factor_registry mismatch"):
        load_strategy_settings(target)


def test_feature_builder_populates_every_tomorrow_component_from_point_in_time_inputs() -> None:
    feature = FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY).build(
        (_quote(),),
        {"600001": _history_bars()},
        AFTERNOON,
        intraday_minutes={"600001": _tail_minute_bars()},
    )[0]

    required = {
        "amount_percentile_20d",
        "relative_strength_5d",
        "relative_strength_20d",
        "price_volume_confirmation",
        "moderate_daily_return",
        "ma20_60_position",
        "ma_slope",
        "breakout_20d",
        "industry_trend",
        "risk_adjusted_return_20d",
        "low_drawdown_score",
        "upward_consistency",
        "capacity_score",
        "moderate_amplitude",
        "limit_distance_safety",
        "tail_return_30m",
        "tail_volume_ratio",
        "close_location",
    }
    assert all(feature.optional_value(name) is not None for name in required)
    assert required.isdisjoint(feature.missing_fields)
