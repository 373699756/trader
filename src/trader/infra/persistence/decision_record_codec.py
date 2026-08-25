"""Current-schema JSON codec for immutable formal decision records."""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Literal, TypeAlias, cast
from zoneinfo import ZoneInfo

from trader.domain.recommendation.decision_identity import (
    COMMITTED_RECORD_SCHEMA_VERSION,
    DECISION_IDENTITY_SCHEMA_VERSION,
    CommitKind,
    CommittedDecisionRecord,
    DecisionDownside,
    DecisionItem,
    DecisionQuote,
    DecisionResearchCoverage,
    DecisionStage,
    ScoredDecision,
    SelectionDiagnostics,
)
from trader.domain.recommendation.models import RecommendationAction, Strategy

_Json: TypeAlias = str | int | float | bool | None | list["_Json"] | dict[str, "_Json"]
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_RECORD_FIELDS = frozenset(
    {
        "schema_version",
        "decision",
        "decision_version",
        "decision_hash",
        "committed_at",
        "commit_kind",
        "payload_hash",
        "version",
    }
)
_DECISION_FIELDS = frozenset(
    {
        "schema_version",
        "strategy",
        "trade_date",
        "sequence",
        "observed_at",
        "stage",
        "parent_version",
        "input_versions",
        "config_version",
        "strategy_version",
        "fusion_version",
        "items",
        "filter_aggregates",
        "degraded_reasons",
        "selection_diagnostics",
    }
)
_DECISION_OPTIONAL_FIELDS = frozenset({"population_count", "rejected_count"})
_ITEM_FIELDS = frozenset(
    {
        "code",
        "action",
        "selected",
        "rank",
        "candidate_score",
        "local_score",
        "final_score",
        "score_components",
        "risk_codes",
        "reason",
        "setup_type",
        "downside",
        "review_outcome",
        "research_coverage",
    }
)
_ITEM_OPTIONAL_FIELDS = frozenset({"name", "industry", "quote"})
_DOWNSIDE_FIELDS = frozenset({"status", "reasons", "atr20_pct", "intraday_reversal_atr", "historical_drawdown_pct"})
_RESEARCH_FIELDS = frozenset({"evidence_count", "structured_risk_fact_count", "review_eligible"})
_SELECTION_FIELDS = frozenset(
    {
        "maximum_final_score",
        "executable_threshold",
        "observation_floor",
        "executable_limit",
        "observation_limit",
        "selected_executable_count",
        "selected_observation_count",
        "review_candidate_count",
        "empty_reason",
    }
)
_QUOTE_FIELDS = frozenset(
    {
        "code",
        "price",
        "pct_change",
        "amount",
        "turnover_rate",
        "market_cap",
        "source",
        "source_time",
        "data_version",
    }
)


def committed_record_bytes(record: CommittedDecisionRecord) -> bytes:
    """Encode one current-schema record using its canonical persisted shape."""

    if record.schema_version != COMMITTED_RECORD_SCHEMA_VERSION:
        raise ValueError("formal decision record does not use the current schema")
    payload = _record_payload(record)
    payload["payload_hash"] = record.payload_hash
    payload["version"] = record.version
    return json.dumps(payload, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")).encode()


def committed_record_from_bytes(payload: bytes) -> CommittedDecisionRecord:
    """Decode and identity-check one current-schema formal record."""

    raw = json.loads(payload.decode("utf-8"))
    value = _object(raw, "formal decision payload", required=_RECORD_FIELDS)
    if _text(value, "schema_version") != COMMITTED_RECORD_SCHEMA_VERSION:
        raise ValueError("formal decision record schema is unsupported")
    decision_raw = _object(
        value.get("decision"),
        "decision",
        required=_DECISION_FIELDS,
        optional=_DECISION_OPTIONAL_FIELDS,
    )
    if _text(decision_raw, "schema_version") != DECISION_IDENTITY_SCHEMA_VERSION:
        raise ValueError("formal decision identity schema is unsupported")
    decision = ScoredDecision(
        strategy=Strategy(_text(decision_raw, "strategy")),
        trade_date=date.fromisoformat(_text(decision_raw, "trade_date")),
        sequence=_integer(decision_raw, "sequence"),
        observed_at=_shanghai_datetime(_text(decision_raw, "observed_at")),
        stage=cast(DecisionStage, _text(decision_raw, "stage")),
        parent_version=_optional_text(decision_raw.get("parent_version")),
        input_versions=_pairs(decision_raw.get("input_versions"), "input_versions"),
        config_version=_text(decision_raw, "config_version"),
        strategy_version=_text(decision_raw, "strategy_version"),
        fusion_version=_text(decision_raw, "fusion_version"),
        items=tuple(_decision_item_from_json(item) for item in _list(decision_raw.get("items"), "items")),
        filter_aggregates=_count_pairs(decision_raw.get("filter_aggregates")),
        degraded_reasons=tuple(_strings(decision_raw.get("degraded_reasons"), "degraded_reasons")),
        population_count=_optional_integer(decision_raw.get("population_count"), "population_count"),
        rejected_count=_optional_integer(decision_raw.get("rejected_count"), "rejected_count"),
        selection_diagnostics=_selection_diagnostics_from_json(decision_raw.get("selection_diagnostics")),
    )
    record = CommittedDecisionRecord(
        decision=decision,
        committed_at=_shanghai_datetime(_text(value, "committed_at")),
        commit_kind=cast(CommitKind, _text(value, "commit_kind")),
    )
    if value.get("decision_version") != decision.version or value.get("decision_hash") != decision.content_hash:
        raise ValueError("formal decision payload decision identity mismatch")
    if value.get("payload_hash") != record.payload_hash or value.get("version") != record.version:
        raise ValueError("formal decision payload identity mismatch")
    return record


def _record_payload(record: CommittedDecisionRecord) -> dict[str, _Json]:
    return {
        "schema_version": record.schema_version,
        "decision": _decision_payload(record.decision),
        "decision_version": record.decision.version,
        "decision_hash": record.decision.content_hash,
        "committed_at": record.committed_at.isoformat(),
        "commit_kind": record.commit_kind,
    }


def _decision_payload(decision: ScoredDecision) -> dict[str, _Json]:
    payload: dict[str, _Json] = {
        "schema_version": decision.schema_version,
        "strategy": decision.strategy.value,
        "trade_date": decision.trade_date.isoformat(),
        "sequence": decision.sequence,
        "observed_at": decision.observed_at.isoformat(),
        "stage": decision.stage,
        "parent_version": decision.parent_version,
        "input_versions": [[name, version] for name, version in decision.input_versions],
        "config_version": decision.config_version,
        "strategy_version": decision.strategy_version,
        "fusion_version": decision.fusion_version,
        "items": [_decision_item_payload(item) for item in decision.items],
        "filter_aggregates": [[reason, count] for reason, count in decision.filter_aggregates],
        "degraded_reasons": list(decision.degraded_reasons),
        "selection_diagnostics": _selection_diagnostics_payload(decision.selection_diagnostics),
    }
    if decision.population_count is not None and decision.rejected_count is not None:
        payload["population_count"] = decision.population_count
        payload["rejected_count"] = decision.rejected_count
    return payload


def _decision_item_payload(item: DecisionItem) -> dict[str, _Json]:
    payload: dict[str, _Json] = {
        "code": item.code,
        "action": item.action.value,
        "selected": item.selected,
        "rank": item.rank,
        "candidate_score": item.candidate_score,
        "local_score": item.local_score,
        "final_score": item.final_score,
        "score_components": [[name, value] for name, value in item.score_components],
        "risk_codes": list(item.risk_codes),
        "reason": item.reason,
        "setup_type": item.setup_type,
        "downside": _downside_payload(item.downside),
        "review_outcome": item.review_outcome,
        "research_coverage": _research_coverage_payload(item.research_coverage),
    }
    if item.name:
        payload["name"] = item.name
    if item.industry:
        payload["industry"] = item.industry
    if item.quote is not None:
        payload["quote"] = _decision_quote_payload(item.quote)
    return payload


def _decision_item_from_json(raw: object) -> DecisionItem:
    value = _object(raw, "decision item", required=_ITEM_FIELDS, optional=_ITEM_OPTIONAL_FIELDS)
    return DecisionItem(
        code=_text(value, "code"),
        action=RecommendationAction(_text(value, "action")),
        selected=_boolean(value, "selected"),
        rank=_integer(value, "rank"),
        candidate_score=_optional_number(value.get("candidate_score")),
        local_score=_number(value, "local_score"),
        final_score=_number(value, "final_score"),
        score_components=_score_pairs(value.get("score_components")),
        risk_codes=tuple(_strings(value.get("risk_codes"), "risk_codes")),
        reason=_text(value, "reason"),
        name=_optional_display_text(value.get("name"), "decision item name"),
        industry=_optional_display_text(value.get("industry"), "decision item industry"),
        quote=_decision_quote_from_json(value.get("quote")),
        setup_type=_optional_text(value.get("setup_type")),
        downside=_downside_from_json(value.get("downside")),
        review_outcome=_optional_text(value.get("review_outcome")),
        research_coverage=_research_coverage_from_json(value.get("research_coverage")),
    )


def _downside_payload(value: DecisionDownside | None) -> dict[str, _Json] | None:
    if value is None:
        return None
    return {
        "status": value.status,
        "reasons": list(value.reasons),
        "atr20_pct": value.atr20_pct,
        "intraday_reversal_atr": value.intraday_reversal_atr,
        "historical_drawdown_pct": value.historical_drawdown_pct,
    }


def _downside_from_json(raw: object) -> DecisionDownside | None:
    if raw is None:
        return None
    value = _object(raw, "decision downside", required=_DOWNSIDE_FIELDS)
    status = _text(value, "status")
    if status not in {"pass", "observe"}:
        raise ValueError("decision downside status is invalid")
    return DecisionDownside(
        cast(Literal["pass", "observe"], status),
        tuple(_strings(value.get("reasons"), "downside reasons")),
        _optional_number(value.get("atr20_pct")),
        _optional_number(value.get("intraday_reversal_atr")),
        _optional_number(value.get("historical_drawdown_pct")),
    )


def _research_coverage_payload(value: DecisionResearchCoverage | None) -> dict[str, _Json] | None:
    if value is None:
        return None
    return {
        "evidence_count": value.evidence_count,
        "structured_risk_fact_count": value.structured_risk_fact_count,
        "review_eligible": value.review_eligible,
    }


def _research_coverage_from_json(raw: object) -> DecisionResearchCoverage | None:
    if raw is None:
        return None
    value = _object(raw, "decision research coverage", required=_RESEARCH_FIELDS)
    return DecisionResearchCoverage(
        _integer(value, "evidence_count"),
        _integer(value, "structured_risk_fact_count"),
        _boolean(value, "review_eligible"),
    )


def _selection_diagnostics_payload(value: SelectionDiagnostics | None) -> dict[str, _Json] | None:
    if value is None:
        return None
    return {
        "maximum_final_score": value.maximum_final_score,
        "executable_threshold": value.executable_threshold,
        "observation_floor": value.observation_floor,
        "executable_limit": value.executable_limit,
        "observation_limit": value.observation_limit,
        "selected_executable_count": value.selected_executable_count,
        "selected_observation_count": value.selected_observation_count,
        "review_candidate_count": value.review_candidate_count,
        "empty_reason": value.empty_reason,
    }


def _selection_diagnostics_from_json(raw: object) -> SelectionDiagnostics | None:
    if raw is None:
        return None
    value = _object(raw, "selection diagnostics", required=_SELECTION_FIELDS)
    return SelectionDiagnostics(
        _optional_number(value.get("maximum_final_score")),
        _number(value, "executable_threshold"),
        _number(value, "observation_floor"),
        _integer(value, "executable_limit"),
        _integer(value, "observation_limit"),
        _integer(value, "selected_executable_count"),
        _integer(value, "selected_observation_count"),
        _integer(value, "review_candidate_count"),
        _optional_text(value.get("empty_reason")),
    )


def _decision_quote_payload(quote: DecisionQuote) -> dict[str, _Json]:
    return {
        "code": quote.code,
        "price": quote.price,
        "pct_change": quote.pct_change,
        "amount": quote.amount,
        "turnover_rate": quote.turnover_rate,
        "market_cap": quote.market_cap,
        "source": quote.source,
        "source_time": quote.source_time.isoformat(),
        "data_version": quote.data_version,
    }


def _decision_quote_from_json(raw: object) -> DecisionQuote | None:
    if raw is None:
        return None
    value = _object(raw, "decision quote", required=_QUOTE_FIELDS)
    return DecisionQuote(
        code=_text(value, "code"),
        price=_number(value, "price"),
        pct_change=_optional_number(value.get("pct_change")),
        amount=_optional_number(value.get("amount")),
        turnover_rate=_optional_number(value.get("turnover_rate")),
        market_cap=_optional_number(value.get("market_cap")),
        source=_text(value, "source"),
        source_time=_shanghai_datetime(_text(value, "source_time")),
        data_version=_text(value, "data_version"),
    )


def _object(
    raw: object,
    label: str,
    *,
    required: frozenset[str],
    optional: frozenset[str] = frozenset(),
) -> dict[str, object]:
    if not isinstance(raw, dict) or any(not isinstance(key, str) for key in raw):
        raise ValueError(f"{label} must be an object")
    value = cast(dict[str, object], raw)
    keys = set(value)
    missing = required - keys
    unknown = keys - required - optional
    if missing:
        raise ValueError(f"{label} is missing fields: {', '.join(sorted(missing))}")
    if unknown:
        raise ValueError(f"{label} has unknown fields: {', '.join(sorted(unknown))}")
    return value


def _list(raw: object, label: str) -> list[object]:
    if not isinstance(raw, list):
        raise ValueError(f"{label} must be a list")
    return raw


def _text(raw: dict[str, object], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} must be non-empty text")
    return value


def _optional_text(raw: object) -> str | None:
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw:
        raise ValueError("optional text is invalid")
    return raw


def _integer(raw: dict[str, object], key: str) -> int:
    value = raw.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{key} must be an integer")
    return value


def _optional_integer(raw: object, label: str) -> int | None:
    if raw is None:
        return None
    if not isinstance(raw, int) or isinstance(raw, bool):
        raise ValueError(f"{label} must be an integer")
    return raw


def _optional_display_text(raw: object, label: str) -> str:
    if raw is None:
        return ""
    if not isinstance(raw, str):
        raise ValueError(f"{label} must be text")
    normalized = raw.strip()
    if len(normalized) > 120 or any(ord(character) < 32 for character in normalized):
        raise ValueError(f"{label} is invalid")
    return normalized


def _boolean(raw: dict[str, object], key: str) -> bool:
    value = raw.get(key)
    if not isinstance(value, bool):
        raise ValueError(f"{key} must be a boolean")
    return value


def _number(raw: dict[str, object], key: str) -> float:
    value = raw.get(key)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(f"{key} must be a number")
    return float(value)


def _optional_number(raw: object) -> float | None:
    if raw is None:
        return None
    if not isinstance(raw, (int, float)) or isinstance(raw, bool):
        raise ValueError("optional number is invalid")
    return float(raw)


def _strings(raw: object, label: str) -> list[str]:
    values = _list(raw, label)
    if any(not isinstance(value, str) for value in values):
        raise ValueError(f"{label} must contain text")
    return cast(list[str], values)


def _pairs(raw: object, label: str) -> tuple[tuple[str, str], ...]:
    pairs: list[tuple[str, str]] = []
    for item in _list(raw, label):
        values = _list(item, label)
        if len(values) != 2 or any(not isinstance(value, str) for value in values):
            raise ValueError(f"{label} entries are invalid")
        pairs.append((cast(str, values[0]), cast(str, values[1])))
    return tuple(pairs)


def _count_pairs(raw: object) -> tuple[tuple[str, int], ...]:
    pairs: list[tuple[str, int]] = []
    for item in _list(raw, "filter_aggregates"):
        values = _list(item, "filter aggregate")
        if (
            len(values) != 2
            or not isinstance(values[0], str)
            or not isinstance(values[1], int)
            or isinstance(values[1], bool)
        ):
            raise ValueError("filter aggregate entries are invalid")
        pairs.append((values[0], values[1]))
    return tuple(pairs)


def _score_pairs(raw: object) -> tuple[tuple[str, float | None], ...]:
    pairs: list[tuple[str, float | None]] = []
    for item in _list(raw, "score_components"):
        values = _list(item, "score component")
        if len(values) != 2 or not isinstance(values[0], str):
            raise ValueError("score component entries are invalid")
        pairs.append((values[0], _optional_number(values[1])))
    return tuple(pairs)


def _shanghai_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("persisted datetime must be timezone-aware")
    return parsed.astimezone(_SHANGHAI)


__all__ = ["committed_record_bytes", "committed_record_from_bytes"]
