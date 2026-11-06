"""Strict configuration boundary for company event and score policies."""

from collections.abc import Mapping

from trader.download.domain.company_events import CompanyEventPolicy
from trader.infra.settings.parser import Boolean, Integer, Number, RequireExactKeys
from trader.recommendation.domain.scoring.company import CompanyScoringPolicy


def ParseCompanyScoringPolicy(raw: Mapping[str, object]) -> CompanyScoringPolicy:
    RequireExactKeys(
        raw,
        {
            "enabled",
            "tomorrow_cap",
            "d25_cap",
            "financial_max_bonus",
            "financial_neutral",
            "financial_weights",
            "positive_points",
            "risk_points",
            "tomorrow_sessions",
            "d25_sessions",
            "share_thresholds",
            "contract_thresholds",
            "risk_days",
            "reduction_tail_days",
            "unlock_days",
        },
        "company_scoring",
    )
    return CompanyScoringPolicy(
        enabled=Boolean(raw, "enabled"),
        tomorrow_cap=Number(raw, "tomorrow_cap", minimum=0, maximum=100),
        d25_cap=Number(raw, "d25_cap", minimum=0, maximum=100),
        financial_max_bonus=Number(raw, "financial_max_bonus", minimum=0, maximum=100),
        financial_neutral=Number(raw, "financial_neutral", minimum=0, maximum=99),
        financial_weights=_triple(raw, "financial_weights"),
        positive_points=_triple(raw, "positive_points"),
        risk_points=_triple(raw, "risk_points"),
        tomorrow_sessions=Integer(raw, "tomorrow_sessions", minimum=1),
        d25_sessions=Integer(raw, "d25_sessions", minimum=1),
        events=CompanyEventPolicy(
            share_thresholds=_triple(raw, "share_thresholds"),
            contract_thresholds=_triple(raw, "contract_thresholds"),
            risk_days=Integer(raw, "risk_days", minimum=1),
            reduction_tail_days=Integer(raw, "reduction_tail_days", minimum=1),
            unlock_days=Integer(raw, "unlock_days", minimum=1),
        ),
    )


def _triple(raw: Mapping[str, object], key: str) -> tuple[float, float, float]:
    value = raw.get(key)
    if not isinstance(value, list) or len(value) != 3:
        raise ValueError(f"company_policy_triple:{key}")
    parsed = tuple(Number({"value": item}, "value", minimum=0, maximum=100) for item in value)
    return parsed[0], parsed[1], parsed[2]
