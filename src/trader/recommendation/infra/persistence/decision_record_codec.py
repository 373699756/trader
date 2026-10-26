"""Current-schema JSON codec for immutable formal decision records."""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Literal, TypeAlias, cast
from zoneinfo import ZoneInfo

from trader.infra.serialization.fields import AsSequence
from trader.recommendation.domain.market.models import Board
from trader.recommendation.domain.market.research import ResearchComponentCoverage, ResearchComponentStatus
from trader.recommendation.domain.publication.decision_identity import (
    COMMITTED_RECORD_SCHEMA_VERSION,
    DECISION_IDENTITY_SCHEMA_VERSION,
    CommitKind,
    CommittedDecisionRecord,
    CommittedRecordIdentityPayload,
    DecisionDownside,
    DecisionItem,
    DecisionModelDiagnostics,
    DecisionQuote,
    DecisionResearchCoverage,
    DecisionRiskDisclosure,
    DecisionStage,
    ScoredDecision,
    SelectionDiagnostics,
)
from trader.recommendation.domain.publication.historical_pipeline import (
    HistoricalFacet,
    HistoricalMetricRange,
    HistoricalPipelineAudit,
    HistoricalPipelineStage,
    HistoricalReasonCount,
    HistoricalSeverity,
    HistoricalSourceHealth,
    HistoricalSourceState,
    HistoricalStageReason,
    HistoricalStageSnapshot,
    HistoricalStageState,
    HistoricalStageStatus,
)
from trader.recommendation.domain.publication.models import RecommendationAction, Strategy
from trader.recommendation.infra.persistence.score_ledger_codec import DecodeScoreLedger

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
_DECISION_OPTIONAL_FIELDS = frozenset({"population_count", "rejected_count", "pipeline", "is_formal"})
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
        "board",
        "selection_rank",
        "setup_type",
        "downside",
        "review_outcome",
        "research_coverage",
    }
)
_ITEM_OPTIONAL_FIELDS = frozenset({"name", "industry", "quote", "model_diagnostics", "risk_details", "score_ledger"})
_DOWNSIDE_FIELDS = frozenset({"status", "reasons", "atr20_pct", "intraday_reversal_atr", "historical_drawdown_pct"})
_RESEARCH_FIELDS = frozenset({"evidence_count", "structured_risk_fact_count", "review_eligible"})
_MODEL_DIAGNOSTIC_FIELDS = frozenset(
    {
        "signal_score",
        "predicted_excess_return_pct",
        "estimated_cost_pct",
        "predicted_net_excess_pct",
    }
)
_MODEL_DIAGNOSTIC_OPTIONAL_FIELDS = frozenset({"model_disagreement_pct"})
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
_SELECTION_OPTIONAL_FIELDS = frozenset({"evaluated_count"})
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
_PIPELINE_FIELDS = frozenset({"current_stage", "stages"})
_PIPELINE_STAGE_FIELDS = frozenset(
    {"key", "state", "input_count", "output_count", "metric_ranges", "threshold", "facets", "reason_counts"}
)
_PIPELINE_RANGE_FIELDS = frozenset({"metric", "minimum", "maximum"})
_PIPELINE_FACET_FIELDS = frozenset({"key", "count", "total"})
_PIPELINE_REASON_FIELDS = frozenset({"reason", "count"})


def CommittedRecordBytes(record: CommittedDecisionRecord) -> bytes:
    """Encode one current-schema record using its canonical persisted shape."""

    if record.schema_version != COMMITTED_RECORD_SCHEMA_VERSION:
        raise ValueError("formal decision record does not use the current schema")
    payload = CommittedRecordIdentityPayload(record)
    payload["payload_hash"] = record.payload_hash
    payload["version"] = record.version
    return json.dumps(payload, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")).encode()


def CommittedRecordFromBytes(payload: bytes) -> CommittedDecisionRecord:
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
        pipeline=_pipeline_from_json(decision_raw.get("pipeline")),
        is_formal=_boolean(decision_raw, "is_formal") if "is_formal" in decision_raw else False,
    )
    persisted_version = _text(value, "decision_version")
    legacy_version = (
        f"decision:{decision.strategy.value}:{decision.trade_date.isoformat()}:"
        f"{decision.stage}:{decision.sequence}:{decision.content_hash[:16]}"
    )
    if persisted_version not in {decision.version, legacy_version}:
        raise ValueError("formal decision payload decision identity mismatch")
    # Immutable historical identities are restored only after integrity verification.
    object.__setattr__(decision, "version", persisted_version)
    record = CommittedDecisionRecord(
        decision=decision,
        committed_at=_shanghai_datetime(_text(value, "committed_at")),
        commit_kind=cast(CommitKind, _text(value, "commit_kind")),
    )
    if value.get("decision_version") != decision.version or value.get("decision_hash") != decision.content_hash:
        raise ValueError("formal decision payload decision identity mismatch")
    persisted_record_version = _text(value, "version")
    legacy_record_version = f"record:{record.strategy.value}:{record.trade_date.isoformat()}:{record.payload_hash[:16]}"
    if value.get("payload_hash") != record.payload_hash or persisted_record_version not in {
        record.version,
        legacy_record_version,
    }:
        raise ValueError("formal decision payload identity mismatch")
    object.__setattr__(record, "version", persisted_record_version)
    return record


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
        score_ledger=DecodeScoreLedger(value["score_ledger"]) if "score_ledger" in value else None,
        risk_codes=tuple(_strings(value.get("risk_codes"), "risk_codes")),
        risk_details=tuple(_risk_detail_from_json(item) for item in _list(value["risk_details"], "risk details"))
        if "risk_details" in value
        else (),
        reason=_text(value, "reason"),
        board=Board(_text(value, "board")),
        selection_rank=_integer(value, "selection_rank"),
        name=_optional_display_text(value.get("name"), "decision item name"),
        industry=_optional_display_text(value.get("industry"), "decision item industry"),
        quote=_decision_quote_from_json(value.get("quote")),
        setup_type=_optional_text(value.get("setup_type")),
        downside=_downside_from_json(value.get("downside")),
        review_outcome=_optional_text(value.get("review_outcome")),
        research_coverage=_research_coverage_from_json(value.get("research_coverage")),
        model_diagnostics=_model_diagnostics_from_json(value.get("model_diagnostics")),
    )


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


def _research_coverage_from_json(raw: object) -> DecisionResearchCoverage | None:
    if raw is None:
        return None
    value = _object(raw, "decision research coverage", required=_RESEARCH_FIELDS, optional=frozenset({"components"}))
    components = None
    if "components" in value:
        detail = _object(
            value["components"],
            "research components",
            required=frozenset({"financial", "announcements", "pledge", "unlock"}),
        )
        components = ResearchComponentCoverage(
            cast("ResearchComponentStatus", _text(detail, "financial")),
            cast("ResearchComponentStatus", _text(detail, "announcements")),
            cast("ResearchComponentStatus", _text(detail, "pledge")),
            cast("ResearchComponentStatus", _text(detail, "unlock")),
        )
    return DecisionResearchCoverage(
        _integer(value, "evidence_count"),
        _integer(value, "structured_risk_fact_count"),
        _boolean(value, "review_eligible"),
        components,
    )


def _risk_detail_from_json(raw: object) -> DecisionRiskDisclosure:
    value = _object(raw, "decision risk detail", required=frozenset({"risk_code", "origin", "veto_applied"}))
    return DecisionRiskDisclosure(
        _text(value, "risk_code"),
        cast(Literal["local", "deepseek"], _text(value, "origin")),
        _boolean(value, "veto_applied"),
    )


def _model_diagnostics_from_json(raw: object) -> DecisionModelDiagnostics | None:
    if raw is None:
        return None
    value = _object(
        raw,
        "decision model diagnostics",
        required=_MODEL_DIAGNOSTIC_FIELDS,
        optional=_MODEL_DIAGNOSTIC_OPTIONAL_FIELDS,
    )
    return DecisionModelDiagnostics(
        _number(value, "signal_score"),
        _number(value, "predicted_excess_return_pct"),
        _number(value, "estimated_cost_pct"),
        _number(value, "predicted_net_excess_pct"),
        _optional_number(value.get("model_disagreement_pct")),
    )


def _selection_diagnostics_from_json(raw: object) -> SelectionDiagnostics | None:
    if raw is None:
        return None
    value = _object(
        raw,
        "selection diagnostics",
        required=_SELECTION_FIELDS,
        optional=_SELECTION_OPTIONAL_FIELDS,
    )
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
        _optional_integer(value.get("evaluated_count"), "evaluated_count"),
    )


def _pipeline_from_json(raw: object) -> HistoricalPipelineAudit | None:
    if raw is None:
        return None
    value = _object(
        raw, "recommendation pipeline", required=_PIPELINE_FIELDS, optional=frozenset({"stage_snapshots", "elapsed_ms"})
    )
    stages = []
    for raw_stage in _list(value.get("stages"), "pipeline stages"):
        stage = _object(raw_stage, "pipeline stage", required=_PIPELINE_STAGE_FIELDS)
        ranges = tuple(
            HistoricalMetricRange(
                _text(item, "metric"),
                _number(item, "minimum"),
                _number(item, "maximum"),
            )
            for item in (
                _object(raw_range, "pipeline metric range", required=_PIPELINE_RANGE_FIELDS)
                for raw_range in _list(stage.get("metric_ranges"), "pipeline metric ranges")
            )
        )
        facets = tuple(
            HistoricalFacet(
                _text(item, "key"),
                _integer(item, "count"),
                _optional_integer(item.get("total"), "pipeline facet total"),
            )
            for item in (
                _object(raw_facet, "pipeline facet", required=_PIPELINE_FACET_FIELDS)
                for raw_facet in _list(stage.get("facets"), "pipeline facets")
            )
        )
        reasons = tuple(
            HistoricalReasonCount(_text(item, "reason"), _integer(item, "count"))
            for item in (
                _object(raw_reason, "pipeline reason", required=_PIPELINE_REASON_FIELDS)
                for raw_reason in _list(stage.get("reason_counts"), "pipeline reasons")
            )
        )
        stages.append(
            HistoricalStageStatus(
                _text(stage, "key"),
                _text(stage, "state"),
                _optional_integer(stage.get("input_count"), "pipeline input count"),
                _optional_integer(stage.get("output_count"), "pipeline output count"),
                ranges,
                _optional_number(stage.get("threshold")),
                facets,
                reasons,
            )
        )
    return HistoricalPipelineAudit(
        _text(value, "current_stage"),
        tuple(stages),
        tuple(_snapshot_from_json(item) for item in _list(value.get("stage_snapshots", []), "stage snapshots")),
        _optional_integer(value.get("elapsed_ms"), "pipeline elapsed time"),
    )


def _snapshot_from_json(raw: object) -> HistoricalStageSnapshot:
    value = _object(
        raw,
        "stage snapshot",
        optional=frozenset({"not_selected_count", "operation_durations"}),
        required=frozenset(
            {
                "stage",
                "stage_order",
                "input_batch_id",
                "output_batch_id",
                "as_of",
                "state",
                "input_count",
                "output_count",
                "rejected_count",
                "pending_count",
                "failed_count",
                "reasons",
                "source_health",
                "latency_ms",
                "degraded",
            }
        ),
    )
    health = _object(
        value.get("source_health"),
        "source health",
        required=frozenset({"state", "source_count", "healthy_source_count", "latest_success_at", "age_seconds"}),
    )
    success = _optional_text(health.get("latest_success_at"))
    reasons = tuple(
        HistoricalStageReason(
            _text(item, "code"),
            _text(item, "label"),
            _integer(item, "count"),
            HistoricalSeverity(_text(item, "severity")),
        )
        for item in (
            _object(reason, "stage reason", required=frozenset({"code", "label", "count", "severity"}))
            for reason in _list(value.get("reasons"), "stage reasons")
        )
    )
    return HistoricalStageSnapshot(
        HistoricalPipelineStage(_text(value, "stage")),
        _integer(value, "stage_order"),
        _text(value, "input_batch_id"),
        _text(value, "output_batch_id"),
        _shanghai_datetime(_text(value, "as_of")),
        HistoricalStageState(_text(value, "state")),
        _integer(value, "input_count"),
        _integer(value, "output_count"),
        _integer(value, "rejected_count"),
        _integer(value, "pending_count"),
        _integer(value, "failed_count"),
        reasons,
        HistoricalSourceHealth(
            HistoricalSourceState(_text(health, "state")),
            _integer(health, "source_count"),
            _integer(health, "healthy_source_count"),
            _shanghai_datetime(success) if success is not None else None,
            _optional_number(health.get("age_seconds")),
        ),
        _optional_integer(value.get("latency_ms"), "latency_ms"),
        _boolean(value, "degraded"),
        _optional_integer(value.get("not_selected_count"), "not_selected_count"),
        tuple(
            (_text(item, "operation"), _optional_integer(item.get("duration_ms"), "operation duration"))
            for item in (
                _object(raw, "operation timing", required=frozenset({"operation", "duration_ms"}))
                for raw in _list(value.get("operation_durations", []), "operation durations")
            )
        ),
    )


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
    sequence = AsSequence(raw)
    if sequence is None:
        raise ValueError(f"{label} must be a list")
    return sequence


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


__all__ = ["CommittedRecordBytes", "CommittedRecordFromBytes"]
