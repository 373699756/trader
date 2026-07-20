from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from trader.application.candidate_features import fetch_strategy_features
from trader.domain.models import Evidence, FeatureSnapshot, Strategy


def test_tomorrow_candidate_input_requests_tail_data_and_versions_it(
    application_feature_factory,
) -> None:
    now = datetime.fromisoformat("2026-07-16T14:30:00+08:00")
    feature = application_feature_factory("600001", now)
    evidence = Evidence(
        "tail-v1",
        "intraday_tail",
        "tail input",
        "eastmoney_intraday",
        now,
        now,
        "intraday-v1",
    )
    market_data = RecordingMarketData((replace(feature, evidence=(evidence,)),))

    _features, first_version = fetch_strategy_features(
        market_data,
        Strategy.TOMORROW,
        ("600001",),
        now,
    )
    market_data.features = (
        replace(feature, evidence=(replace(evidence, evidence_id="tail-v2", data_version="intraday-v2"),)),
    )
    _features, second_version = fetch_strategy_features(
        market_data,
        Strategy.TOMORROW,
        ("600001",),
        now,
    )
    research = Evidence(
        "financial-v1",
        "financial_snapshot",
        "point-in-time financial input",
        "eastmoney_financial",
        now,
        now,
        "financial-v1",
    )
    market_data.features = (replace(feature, evidence=(research,)),)
    _features, d25_version = fetch_strategy_features(market_data, Strategy.D25, ("600001",), now)
    market_data.features = (
        replace(feature, evidence=(replace(research, evidence_id="financial-v2", data_version="financial-v2"),)),
    )
    _features, changed_d25_version = fetch_strategy_features(market_data, Strategy.D25, ("600001",), now)
    _features, long_version = fetch_strategy_features(market_data, Strategy.LONG, ("600001",), now)

    assert market_data.tail_requests == [True, True, False, False, False]
    assert market_data.structured_requests == [True, True, True, True, True]
    assert first_version.startswith("tomorrow-input:")
    assert second_version != first_version
    assert d25_version.startswith("d25-input:")
    assert changed_d25_version != d25_version
    assert long_version.startswith("long-input:")


class RecordingMarketData:
    def __init__(self, features: tuple[FeatureSnapshot, ...]) -> None:
        self.features = features
        self.tail_requests: list[bool] = []
        self.structured_requests: list[bool] = []

    @staticmethod
    def fetch_market_features(_observed_at: datetime) -> tuple[FeatureSnapshot, ...]:
        return ()

    def fetch_candidate_features(
        self,
        _codes: tuple[str, ...],
        _observed_at: datetime,
        *,
        include_intraday_tail: bool = False,
        include_structured_research: bool = False,
    ) -> tuple[FeatureSnapshot, ...]:
        self.tail_requests.append(include_intraday_tail)
        self.structured_requests.append(include_structured_research)
        return self.features

    @staticmethod
    def health() -> dict[str, object]:
        return {}
