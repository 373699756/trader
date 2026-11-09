"""Synthetic registrations, never evidence of real point-in-time qualification."""

from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo
from typing import Literal, cast

from trader.infra.serialization.canonical import CanonicalJsonText
from trader.recommendation.domain.scoring.profile_identity import ScoringProfileId
from trader.training.domain.evaluation.research_registration import (
    CandidateResearchRegistration, ResearchModelComponent, ResearchParameter, ResearchStateDay,
    ResearchStateRegistration, ResearchStateRule, ResearchStrategy,
)


def CandidateRegistrationFixture(
    profile_id: ScoringProfileId = "v2", strategy: ResearchStrategy = "tomorrow", *, model_hash: str = "c" * 64,
    qualification_hash: str = "3" * 64, population_hash: str = "f" * 64, fit_dates: int = 750,
) -> CandidateResearchRegistration:
    families: dict[ScoringProfileId, Literal["ridge", "lightgbm", "tcn"]] = {"v1": "ridge", "v2": "lightgbm", "v3": "ridge", "v5": "tcn"}
    family = families[profile_id]
    utility = ResearchModelComponent(
        "utility", family, model_hash, "4" * 64, "5" * 64,
        60 if profile_id == "v5" else 0, 10 if profile_id == "v5" else 0,
    )
    components = (
        (ResearchModelComponent("ranker", "lambdarank", "9" * 64, "4" * 64, "5" * 64, 0, 0), utility)
        if profile_id == "v1" else (utility,)
    )
    return CandidateResearchRegistration(
        profile_id, strategy, "window_candidate", ("window_candidate",), "fit_dates",
        (ResearchParameter("fit_dates", fit_dates, "trading_days"),), "confirm_once",
        datetime(2020, 1, 1, tzinfo=ZoneInfo("Asia/Shanghai")), "6" * 64, qualification_hash,
        population_hash, "7" * 64, components, components,
    )


def StateRegistrationFixture(
    dates: tuple[date, ...], *, qualification_hash: str = "3" * 64, vary: bool = False,
) -> ResearchStateRegistration:
    days = []
    for index, day in enumerate(dates):
        anchor = datetime.combine(day, time(15), ZoneInfo("Asia/Shanghai"))
        days.append(ResearchStateDay(
            day, anchor, anchor - timedelta(seconds=1), f"fixture_record_{day:%Y%m%d}", "8" * 64,
            (1.0, -1.0, 0.0)[index % 3] if vary else 1.0,
            float(index % 2) if vary else 0.0, float(1 - index % 2) if vary else 1.0,
        ))
    return ResearchStateRegistration(
        "strategy_trading_day", "synthetic_state_generator", "a" * 64, qualification_hash,
        datetime(2020, 1, 1, tzinfo=ZoneInfo("Asia/Shanghai")),
        ResearchStateRule("fixture_market", 20, (-0.5, 0.5), ("down", "sideways", "up"), "upper_inclusive", unit="fixture_ratio"),
        ResearchStateRule("fixture_volatility", 20, (0.5,), ("low", "high"), "upper_inclusive", unit="fixture_ratio"),
        ResearchStateRule("fixture_liquidity", 20, (0.5,), ("low", "high"), "upper_inclusive", unit="fixture_ratio"), tuple(days),
    )


def RegistrationPayloadFixture(value: CandidateResearchRegistration | ResearchStateRegistration) -> dict[str, object]:
    return cast(dict[str, object], json.loads(CanonicalJsonText(value)))
