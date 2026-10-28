"""Strict score ledger decoder; absent legacy ledgers are handled by the freeze codec."""

from __future__ import annotations

import math
from datetime import date, datetime

from trader.recommendation.domain.publication.score_ledger import ScoreAdjustmentItem, ScoreLedger


def DecodeScoreLedger(raw: object) -> ScoreLedger:
    value = _object(raw)
    items = value.get("items")
    if not isinstance(items, list) or type(value.get("enabled")) is not bool:
        raise ValueError("score_ledger_structure_invalid")
    level = value.get("company_risk_level")
    if level is not None and (type(level) is not int or not 0 <= level <= 4):
        raise ValueError("company_risk_level_invalid")
    coverage = value.get("company_coverage_end")
    if coverage is not None and not isinstance(coverage, str):
        raise ValueError("company_coverage_end_invalid")
    return ScoreLedger(
        weighted_base_score=_number(value, "weighted_base_score"),
        requested_bonus=_number(value, "requested_bonus"),
        capped_bonus=_number(value, "capped_bonus"),
        applied_bonus=_number(value, "applied_bonus"),
        base_score=_number(value, "base_score"),
        raw_risk_penalty=_number(value, "raw_risk_penalty"),
        local_risk_penalty=_number(value, "local_risk_penalty"),
        applied_risk_penalty=_number(value, "applied_risk_penalty"),
        local_score=_number(value, "local_score"),
        rounding_adjustment=_number(value, "rounding_adjustment"),
        financial_contribution=_number(value, "financial_contribution"),
        growth_contribution=_number(value, "growth_contribution"),
        company_risk_penalty=_number(value, "company_risk_penalty"),
        company_risk_level=level,
        company_coverage_end=date.fromisoformat(coverage) if coverage else None,
        enabled=value["enabled"] is True,
        items=tuple(_item(item) for item in items),
    )


def _item(raw: object) -> ScoreAdjustmentItem:
    value = _object(raw)
    evidence = value.get("evidence_ids")
    level = value.get("level")
    until = value.get("expires_at")
    if not isinstance(evidence, list) or not all(isinstance(item, str) for item in evidence):
        raise ValueError("score_ledger_evidence_invalid")
    if type(level) is not int or not 0 <= level <= 4:
        raise ValueError("score_ledger_level_invalid")
    if until is not None and not isinstance(until, str):
        raise ValueError("score_ledger_expiry_invalid")
    return ScoreAdjustmentItem(
        _text(value, "code"),
        _text(value, "kind"),
        _text(value, "label"),
        level,
        _number(value, "rule_points"),
        _number(value, "applied_points"),
        _text(value, "reason"),
        tuple(evidence),
        datetime.fromisoformat(until) if until else None,
    )


def _object(raw: object) -> dict[str, object]:
    if not isinstance(raw, dict) or any(not isinstance(key, str) for key in raw):
        raise ValueError("score_ledger_object_invalid")
    return raw


def _number(value: dict[str, object], key: str) -> float:
    result = value.get(key)
    if isinstance(result, bool) or not isinstance(result, (float, int)) or not math.isfinite(result):
        raise ValueError(f"score_ledger_number_invalid:{key}")
    return float(result)


def _text(value: dict[str, object], key: str) -> str:
    result = value.get(key)
    if not isinstance(result, str) or not result:
        raise ValueError(f"score_ledger_text_invalid:{key}")
    return result
