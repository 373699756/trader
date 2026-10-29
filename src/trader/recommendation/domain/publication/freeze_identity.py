"""Pure unified freeze, projection, overlay, and formal-record identities."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from functools import cached_property
from typing import Literal, TypeAlias
from zoneinfo import ZoneInfo

from trader.infra.serialization.canonical_codec import CanonicalDigest
from trader.recommendation.domain.evidence.pipeline import PipelineStageSnapshot, RecommendationPipelineStatus
from trader.recommendation.domain.market.models import Board
from trader.recommendation.domain.market.research import ResearchComponentCoverage
from trader.recommendation.domain.publication.historical_pipeline import (
    HistoricalPipelineAudit,
    HistoricalStageSnapshot,
)
from trader.recommendation.domain.publication.models import RecommendationAction, Strategy
from trader.recommendation.domain.publication.preview import PreviewContext
from trader.recommendation.domain.publication.score_ledger import ScoreLedger, ScoreLedgerPayload
from trader.recommendation.domain.scoring.profile_identity import (
    LEARNED_RANK_SCORE_SCALE,
    MODEL_RANK_SCORE_SCALES,
)

FreezeStage = Literal["local", "hybrid"]
CommitKind = Literal["scheduled", "close_fallback"]
FREEZE_IDENTITY_SCHEMA_VERSION = "freeze_identity"
LONG_PROJECTION_SCHEMA_VERSION = "long_projection"
OVERLAY_SCHEMA_VERSION = "freeze_overlay"
COMMITTED_RECORD_SCHEMA_VERSION = "committed_freeze"
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_CODE = re.compile(r"^\d{6}$")
_IDENTITY = re.compile(r"^[a-zA-Z0-9_.:+-]{1,160}$")
_REASON = re.compile(r"^[a-z0-9_]{1,64}$")
_ACTION_REASON = re.compile(r"^[a-z0-9_,:]{1,160}$")
_Json: TypeAlias = str | int | float | bool | None | list["_Json"] | dict[str, "_Json"]


@dataclass(frozen=True)
class FreezeQuote:
    """Complete immutable quote facts used by scored identities and overlays."""

    code: str
    price: float
    pct_change: float | None
    amount: float | None
    turnover_rate: float | None
    market_cap: float | None
    source: str
    source_time: datetime
    data_version: str

    def __post_init__(self) -> None:
        _require_code(self.code)
        _validate_optional_market_value(self.price, "freeze quote price", positive=True)
        _validate_optional_market_value(self.pct_change, "freeze quote pct_change")
        _validate_optional_market_value(self.amount, "freeze quote amount", non_negative=True)
        _validate_optional_market_value(self.turnover_rate, "freeze quote turnover_rate", non_negative=True)
        _validate_optional_market_value(self.market_cap, "freeze quote market_cap", non_negative=True)
        _require_identity(self.source, "freeze quote source")
        _require_identity(self.data_version, "freeze quote data version")
        _require_shanghai(self.source_time, "freeze quote source_time")


@dataclass(frozen=True)
class FreezeDownside:
    status: Literal["pass", "observe"]
    reasons: tuple[str, ...]
    atr20_pct: float | None
    intraday_reversal_atr: float | None
    historical_drawdown_pct: float | None

    def __post_init__(self) -> None:
        if self.status not in {"pass", "observe"}:
            raise ValueError("freeze downside status is invalid")
        object.__setattr__(self, "reasons", _normalize_reasons(self.reasons))
        for value in (self.atr20_pct, self.intraday_reversal_atr, self.historical_drawdown_pct):
            if value is not None and not math.isfinite(value):
                raise ValueError("freeze downside metrics must be finite")


@dataclass(frozen=True)
class FreezeResearchCoverage:
    evidence_count: int
    structured_risk_fact_count: int
    review_eligible: bool
    components: ResearchComponentCoverage | None = None

    def __post_init__(self) -> None:
        if self.evidence_count < 0 or self.structured_risk_fact_count < 0:
            raise ValueError("freeze research coverage counts cannot be negative")


@dataclass(frozen=True)
class FreezeRiskDisclosure:
    risk_code: str
    origin: Literal["local", "deepseek"]
    veto_applied: bool

    def __post_init__(self) -> None:
        if _REASON.fullmatch(self.risk_code) is None or self.origin not in {"local", "deepseek"}:
            raise ValueError("freeze risk disclosure is invalid")


@dataclass(frozen=True)
class SelectionDiagnostics:
    maximum_final_score: float | None
    executable_threshold: float
    observation_floor: float
    executable_limit: int
    observation_limit: int
    selected_executable_count: int
    selected_observation_count: int
    review_candidate_count: int
    empty_reason: str | None = None
    evaluated_count: int | None = None

    def __post_init__(self) -> None:
        _validate_optional_score(self.maximum_final_score, "maximum final score")
        _validate_score(self.executable_threshold, "selection executable threshold")
        _validate_score(self.observation_floor, "selection observation floor")
        if self.observation_floor > self.executable_threshold:
            raise ValueError("selection observation floor cannot exceed executable threshold")
        if (
            min(
                self.executable_limit,
                self.observation_limit,
                self.selected_executable_count,
                self.selected_observation_count,
                self.review_candidate_count,
                self.evaluated_count if self.evaluated_count is not None else 0,
            )
            < 0
        ):
            raise ValueError("selection diagnostic counts cannot be negative")
        if self.evaluated_count is not None:
            if (self.evaluated_count == 0) != (self.maximum_final_score is None):
                raise ValueError("selection maximum score must match evaluated candidates")
            if (
                max(
                    self.selected_executable_count + self.selected_observation_count,
                    self.review_candidate_count,
                )
                > self.evaluated_count
            ):
                raise ValueError("selection counts cannot exceed evaluated candidates")
        if self.empty_reason is not None and _REASON.fullmatch(self.empty_reason) is None:
            raise ValueError("selection empty reason must be structured")


@dataclass(frozen=True)
class FreezeModelDiagnostics:
    signal_score: float
    predicted_excess_return_pct: float
    estimated_cost_pct: float
    predicted_net_excess_pct: float
    model_disagreement_pct: float | None = None

    def __post_init__(self) -> None:
        _validate_score(self.signal_score, "model signal score")
        _validate_optional_market_value(self.predicted_excess_return_pct, "predicted excess return")
        _validate_optional_market_value(self.estimated_cost_pct, "estimated model cost", non_negative=True)
        _validate_optional_market_value(self.predicted_net_excess_pct, "predicted net excess")
        if self.model_disagreement_pct is not None:
            _validate_optional_market_value(self.model_disagreement_pct, "model disagreement", non_negative=True)


@dataclass(frozen=True)
class FreezeItem:
    code: str
    action: RecommendationAction
    selected: bool
    rank: int
    candidate_score: float | None
    local_score: float
    final_score: float
    score_components: tuple[tuple[str, float | None], ...]
    risk_codes: tuple[str, ...]
    reason: str
    board: Board
    selection_rank: int
    name: str = ""
    industry: str = ""
    quote: FreezeQuote | None = None
    setup_type: str | None = None
    downside: FreezeDownside | None = None
    review_outcome: str | None = None
    research_coverage: FreezeResearchCoverage | None = None
    model_diagnostics: FreezeModelDiagnostics | None = None
    risk_details: tuple[FreezeRiskDisclosure, ...] = ()
    score_ledger: ScoreLedger | None = None

    def __post_init__(self) -> None:
        _require_code(self.code)
        _validate_optional_score(self.candidate_score, "candidate_score")
        _validate_score(self.local_score, "local_score")
        _validate_score(self.final_score, "final_score")
        components = tuple(sorted(self.score_components))
        if not components or len({name for name, _value in components}) != len(components):
            raise ValueError("freeze score components must be non-empty and unique")
        for name, value in components:
            _require_identity(name, "score component")
            _validate_optional_score(value, "score component")
        risks = tuple(sorted(set(self.risk_codes)))
        if any(_REASON.fullmatch(value) is None for value in risks):
            raise ValueError("freeze risk codes must be structured")
        if self.risk_details and set(detail.risk_code for detail in self.risk_details) != set(risks):
            raise ValueError("freeze risk disclosure must match risk codes")
        if _ACTION_REASON.fullmatch(self.reason) is None:
            raise ValueError("freeze reason must be structured")
        _validate_freeze_item_selection(self)
        name = _display_text(self.name, "freeze item name")
        industry = _display_text(self.industry, "freeze item industry")
        if self.quote is not None and self.quote.code != self.code:
            raise ValueError("freeze quote code must match item code")
        if self.setup_type is not None:
            _require_identity(self.setup_type, "freeze setup type")
        if self.review_outcome is not None:
            _require_identity(self.review_outcome, "freeze review outcome")
        object.__setattr__(self, "score_components", components)
        object.__setattr__(self, "risk_codes", risks)
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "industry", industry)


@dataclass(frozen=True)
class ScoredFreeze:
    strategy: Strategy
    trade_date: date
    sequence: int
    observed_at: datetime
    stage: FreezeStage
    parent_version: str | None
    input_versions: tuple[tuple[str, str], ...]
    config_version: str
    strategy_version: str
    fusion_version: str
    items: tuple[FreezeItem, ...]
    filter_aggregates: tuple[tuple[str, int], ...]
    degraded_reasons: tuple[str, ...] = ()
    population_count: int | None = None
    rejected_count: int | None = None
    selection_diagnostics: SelectionDiagnostics | None = None
    pipeline: RecommendationPipelineStatus | HistoricalPipelineAudit | None = None
    schema_version: str = FREEZE_IDENTITY_SCHEMA_VERSION
    is_formal: bool = field(default=False, kw_only=True)
    preview_context: PreviewContext | None = field(default=None, kw_only=True)
    version: str = field(init=False)

    @cached_property
    def content_hash(self) -> str:
        """Integrity reference, materialized only by persistence/audit boundaries."""
        return _hash(
            _scored_payload(self, self.items, self.input_versions, self.filter_aggregates, self.degraded_reasons)
        )

    def __post_init__(self) -> None:
        if self.strategy not in {Strategy.TOMORROW, Strategy.D25}:
            raise ValueError("scored strategy must be tomorrow or d25")
        if self.pipeline is not None and not isinstance(
            self.pipeline, (RecommendationPipelineStatus, HistoricalPipelineAudit)
        ):
            raise ValueError("freeze pipeline must be an active status or a historical audit")
        _validate_coordinates(self.trade_date, self.sequence, self.observed_at)
        _validate_scored_schema(self.schema_version, self.selection_diagnostics, self.items)
        if self.stage == "local" and self.parent_version is not None:
            raise ValueError("local freeze cannot reference a parent")
        if self.stage == "hybrid" and not self.parent_version:
            raise ValueError("hybrid freeze requires a parent version")
        if self.stage not in {"local", "hybrid"}:
            raise ValueError("freeze stage is invalid")
        versions = _preview_input_versions(self, _normalize_versions(self.input_versions))
        _require_identity(self.config_version, "config version")
        _require_identity(self.strategy_version, "strategy version")
        _require_identity(self.fusion_version, "fusion version")
        items = tuple(sorted(self.items, key=lambda item: item.code))
        if len({item.code for item in items}) != len(items):
            raise ValueError("freeze items must contain unique codes")
        selected = sorted((item for item in items if item.selected), key=lambda item: item.rank)
        if [item.rank for item in selected] != list(range(1, len(selected) + 1)):
            raise ValueError("selected freeze ranks must be contiguous")
        if dict(versions).get("execution_mode") == "shadow" and (
            dict(versions).get("production_authority") != "false"
            or self.is_formal
            or any(item.action is RecommendationAction.EXECUTABLE for item in items)
        ):
            raise ValueError("shadow identity cannot grant production authority or executable actions")
        score_scale = dict(versions).get("score_scale")
        model_rank = score_scale in MODEL_RANK_SCORE_SCALES
        if model_rank:
            score_model_id = dict(versions).get("score_model_id")
            if (
                (dict(versions).get("execution_mode"), dict(versions).get("production_authority"))
                not in {("shadow", "false"), ("production", "true")}
                or not score_model_id
                or (score_scale == LEARNED_RANK_SCORE_SCALE and score_model_id != "learned_rank")
                or not all(
                    dict(versions).get(name) for name in ("score_model", "score_contract_hash", "rank_population")
                )
            ):
                raise ValueError("model rank freezes require a complete execution and model identity")
        elif self.stage == "local" and any(item.final_score != item.local_score for item in items):
            raise ValueError("local freeze final scores must equal local scores")
        if any(item.quote is not None and item.quote.source_time > self.observed_at for item in items):
            raise ValueError("freeze cannot contain a future quote")
        aggregates = _normalize_counts(self.filter_aggregates)
        reasons = _normalize_reasons(self.degraded_reasons)
        _validate_coverage_counts(self.population_count, self.rejected_count, len(items))
        object.__setattr__(self, "input_versions", versions)
        object.__setattr__(self, "items", items)
        object.__setattr__(self, "filter_aggregates", aggregates)
        object.__setattr__(self, "degraded_reasons", reasons)
        object.__setattr__(
            self,
            "version",
            f"freeze:{self.strategy.value}:{self.trade_date.isoformat()}:{self.stage}:{self.sequence}"
            + (f":preview:{self.preview_context.run_id}" if self.preview_context is not None else "")
            + (":formal" if self.is_formal else ""),
        )


@dataclass(frozen=True)
class LongProjectionItem:
    code: str
    group: str
    quote_version: str
    name: str = ""
    industry: str = ""
    price: float | None = None
    pct_change: float | None = None
    amount: float | None = None
    turnover_rate: float | None = None
    market_cap: float | None = None
    source: str = ""
    source_time: datetime | None = None
    quote_status: Literal["live", "retained", "missing"] = "missing"

    def __post_init__(self) -> None:
        _require_code(self.code)
        _require_identity(self.group, "long group")
        _require_identity(self.quote_version, "long quote version")
        if self.quote_status not in {"live", "retained", "missing"}:
            raise ValueError("long quote status is invalid")
        _validate_optional_market_value(self.price, "long price", positive=True)
        _validate_optional_market_value(self.pct_change, "long pct_change")
        _validate_optional_market_value(self.amount, "long amount", non_negative=True)
        _validate_optional_market_value(self.turnover_rate, "long turnover_rate", non_negative=True)
        _validate_optional_market_value(self.market_cap, "long market_cap", non_negative=True)
        if self.quote_status == "missing":
            if (
                any(
                    value is not None
                    for value in (self.price, self.pct_change, self.amount, self.turnover_rate, self.market_cap)
                )
                or self.source_time is not None
            ):
                raise ValueError("missing long quote cannot contain market values")
        else:
            if not self.name or not self.source or self.source_time is None or self.price is None:
                raise ValueError("available long quote requires name, source, time, and price")
            _require_identity(self.source, "long quote source")
            _require_shanghai(self.source_time, "long quote source_time")


@dataclass(frozen=True)
class LongProjection:
    trade_date: date
    sequence: int
    observed_at: datetime
    input_versions: tuple[tuple[str, str], ...]
    items: tuple[LongProjectionItem, ...]
    schema_version: str = LONG_PROJECTION_SCHEMA_VERSION
    version: str = field(init=False)
    strategy: Strategy = field(init=False, default=Strategy.LONG)

    def __post_init__(self) -> None:
        _validate_coordinates(self.trade_date, self.sequence, self.observed_at)
        if self.schema_version != LONG_PROJECTION_SCHEMA_VERSION:
            raise ValueError(f"long schema_version must be {LONG_PROJECTION_SCHEMA_VERSION}")
        versions = _normalize_versions(self.input_versions)
        items = tuple(self.items)
        if len({item.code for item in items}) != len(items):
            raise ValueError("long projection items must contain unique codes")
        if any(item.source_time is not None and item.source_time > self.observed_at for item in items):
            raise ValueError("long projection cannot contain future quotes")
        object.__setattr__(self, "input_versions", versions)
        object.__setattr__(self, "items", items)
        object.__setattr__(
            self,
            "version",
            f"projection:long:{self.trade_date.isoformat()}:{self.sequence}",
        )


FreezeIdentity: TypeAlias = ScoredFreeze | LongProjection


@dataclass(frozen=True)
class FreezeOverlay:
    sequence: int = field(kw_only=True)
    strategy: Strategy
    trade_date: date
    parent_version: str
    observed_at: datetime
    quotes: tuple[FreezeQuote, ...]
    schema_version: str = OVERLAY_SCHEMA_VERSION
    version: str = field(init=False)

    def __post_init__(self) -> None:
        _validate_coordinates(self.trade_date, self.sequence, self.observed_at)
        if self.schema_version != OVERLAY_SCHEMA_VERSION:
            raise ValueError(f"overlay schema_version must be {OVERLAY_SCHEMA_VERSION}")
        if self.strategy not in set(Strategy):
            raise ValueError("overlay strategy is invalid")
        _require_identity(self.parent_version, "overlay parent version")
        _require_shanghai(self.observed_at, "overlay observed_at")
        if self.observed_at.date() != self.trade_date:
            raise ValueError("overlay and observation must share a trade date")
        quotes = tuple(sorted(self.quotes, key=lambda quote: quote.code))
        if len({quote.code for quote in quotes}) != len(quotes):
            raise ValueError("overlay quotes must contain unique codes")
        if any(quote.source_time > self.observed_at for quote in quotes):
            raise ValueError("overlay cannot contain a future quote")
        object.__setattr__(self, "quotes", quotes)
        object.__setattr__(
            self,
            "version",
            f"overlay:{self.parent_version}:{self.sequence}",
        )


@dataclass(frozen=True)
class CommittedFreezeRecord:
    freeze: ScoredFreeze
    committed_at: datetime
    commit_kind: CommitKind
    schema_version: str = COMMITTED_RECORD_SCHEMA_VERSION

    @cached_property
    def payload_hash(self) -> str:
        return _hash(CommittedRecordIdentityPayload(self))

    version: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != COMMITTED_RECORD_SCHEMA_VERSION:
            raise ValueError(f"record schema_version must be {COMMITTED_RECORD_SCHEMA_VERSION}")
        if IsPreviewFreeze(self.freeze):
            raise ValueError("preview freezes cannot become formal records")
        if IsShadowFreeze(self.freeze):
            raise ValueError("shadow freezes cannot become formal records")
        _require_shanghai(self.committed_at, "freeze committed_at")
        if self.committed_at.date() != self.freeze.trade_date:
            raise ValueError("formal record and freeze must share a trade date")
        if self.committed_at < self.freeze.observed_at:
            raise ValueError("formal record cannot predate its freeze")
        if self.commit_kind not in {"scheduled", "close_fallback"}:
            raise ValueError("formal freeze commit kind is invalid")
        object.__setattr__(
            self,
            "version",
            f"record:{self.strategy.value}:{self.trade_date.isoformat()}:{self.freeze.stage}:{self.freeze.sequence}",
        )

    @property
    def strategy(self) -> Strategy:
        return self.freeze.strategy

    @property
    def trade_date(self) -> date:
        return self.freeze.trade_date


def IdentityCodes(identity: FreezeIdentity) -> frozenset[str]:
    if isinstance(identity, ScoredFreeze):
        return frozenset(item.code for item in identity.items if item.selected)
    return frozenset(item.code for item in identity.items)


def IsShadowFreeze(freeze: ScoredFreeze) -> bool:
    """Read explicit execution identity without reinterpreting a profile name."""

    return ("execution_mode", "shadow") in freeze.input_versions


def IsPreviewFreeze(freeze: ScoredFreeze) -> bool:
    """Read an explicit ephemeral run, independently of algorithm or profile identity."""
    return freeze.preview_context is not None


def _preview_input_versions(freeze: ScoredFreeze, versions: tuple[tuple[str, str], ...]) -> tuple[tuple[str, str], ...]:
    context = freeze.preview_context
    if context is None:
        if ("run_mode", "preview") in versions:
            raise ValueError("preview freeze identity requires its typed run context")
        return versions
    if freeze.is_formal or context.started_at > freeze.observed_at:
        raise ValueError("preview freezes require a current non-formal run context")
    if any(
        item.quote is not None and not context.accepts_quote(item.quote.source_time, freeze.observed_at)
        for item in freeze.items
    ):
        raise ValueError("preview freeze quotes must belong to its completed market session")
    expected = (
        ("run_mode", "preview"),
        ("preview_run_id", context.run_id),
        ("market_trade_date", context.market_trade_date.isoformat()),
    )
    for name, value in expected:
        supplied = next((version for key, version in versions if key == name), None)
        if supplied is not None and supplied != value:
            raise ValueError("preview context conflicts with freeze input identity")
        if supplied is None:
            versions = (*versions, (name, value))
    return _normalize_versions(versions)


def VisibleScoredSeats(freeze: ScoredFreeze) -> tuple[FreezeItem, ...]:
    """Read fixed seats; retain the original display contract only for old records."""
    audit = freeze.pipeline
    legacy = isinstance(audit, HistoricalPipelineAudit) and audit.stages[0].key != "local_data"
    if legacy or (freeze.is_formal and audit is None):
        return tuple(sorted(freeze.items, key=lambda item: (-item.final_score, -item.local_score, item.code))[:6])
    return tuple(sorted((item for item in freeze.items if item.selected), key=lambda item: item.rank))


def DisplayQuoteCodes(freeze: ScoredFreeze) -> frozenset[str]:
    """Return the scored codes visible in the current six-seat table."""

    return frozenset(item.code for item in VisibleScoredSeats(freeze))


def FormalScoredFreeze(
    freeze: ScoredFreeze,
    *,
    degraded_reasons: tuple[str, ...] = (),
    input_versions: tuple[tuple[str, str], ...] = (),
) -> ScoredFreeze:
    """Project an accepted scored identity to its immutable six-row close form."""

    if IsPreviewFreeze(freeze):
        raise ValueError("preview freezes cannot become formal freezes")
    if IsShadowFreeze(freeze):
        raise ValueError("shadow freezes cannot become formal freezes")

    official_items = VisibleScoredSeats(freeze)
    return ScoredFreeze(
        strategy=freeze.strategy,
        trade_date=freeze.trade_date,
        sequence=freeze.sequence,
        observed_at=freeze.observed_at,
        stage=freeze.stage,
        parent_version=freeze.parent_version,
        input_versions=(*freeze.input_versions, *input_versions),
        config_version=freeze.config_version,
        strategy_version=freeze.strategy_version,
        fusion_version=freeze.fusion_version,
        items=official_items,
        filter_aggregates=freeze.filter_aggregates,
        degraded_reasons=(*freeze.degraded_reasons, *degraded_reasons),
        population_count=freeze.population_count,
        rejected_count=freeze.rejected_count,
        selection_diagnostics=freeze.selection_diagnostics,
        pipeline=freeze.pipeline,
        schema_version=freeze.schema_version,
        is_formal=True,
    )


def _scored_payload(
    freeze: ScoredFreeze,
    items: tuple[FreezeItem, ...],
    versions: tuple[tuple[str, str], ...],
    aggregates: tuple[tuple[str, int], ...],
    reasons: tuple[str, ...],
) -> dict[str, _Json]:
    payload: dict[str, _Json] = {
        "schema_version": freeze.schema_version,
        "strategy": freeze.strategy.value,
        "trade_date": freeze.trade_date.isoformat(),
        "sequence": freeze.sequence,
        "observed_at": freeze.observed_at.isoformat(),
        "stage": freeze.stage,
        "parent_version": freeze.parent_version,
        "input_versions": [[name, version] for name, version in versions],
        "config_version": freeze.config_version,
        "strategy_version": freeze.strategy_version,
        "fusion_version": freeze.fusion_version,
        "items": [_freeze_item_payload(item) for item in items],
        "filter_aggregates": [[reason, count] for reason, count in aggregates],
        "degraded_reasons": list(reasons),
    }
    if freeze.is_formal:
        payload["is_formal"] = True
    if freeze.preview_context is not None:
        payload["preview_context"] = {
            "run_id": freeze.preview_context.run_id,
            "market_trade_date": freeze.preview_context.market_trade_date.isoformat(),
            "started_at": freeze.preview_context.started_at.isoformat(),
        }
    if freeze.population_count is not None and freeze.rejected_count is not None:
        payload["population_count"] = freeze.population_count
        payload["rejected_count"] = freeze.rejected_count
    payload["selection_diagnostics"] = _selection_diagnostics_payload(freeze.selection_diagnostics)
    if isinstance(freeze.pipeline, HistoricalPipelineAudit):
        payload["pipeline"] = _historical_pipeline_payload(freeze.pipeline)
    elif freeze.pipeline is not None:
        payload["pipeline"] = _pipeline_payload(freeze.pipeline)
    return payload


def _pipeline_payload(pipeline: RecommendationPipelineStatus) -> dict[str, _Json]:
    payload: dict[str, _Json] = {
        "current_stage": pipeline.current_stage,
        "stages": [
            {
                "key": stage.key,
                "state": stage.state,
                "input_count": stage.input_count,
                "output_count": stage.output_count,
                "metric_ranges": [
                    {"metric": value.metric, "minimum": value.minimum, "maximum": value.maximum}
                    for value in stage.metric_ranges
                ],
                "threshold": stage.threshold,
                "facets": [{"key": value.key, "count": value.count, "total": value.total} for value in stage.facets],
                "reason_counts": [{"reason": value.reason, "count": value.count} for value in stage.reason_counts],
            }
            for stage in pipeline.stages
        ],
    }
    # Absence is part of legacy identities: never rehash old records by adding an empty audit.
    if pipeline.elapsed_ms is not None:
        payload["elapsed_ms"] = pipeline.elapsed_ms
    if pipeline.stage_snapshots:
        payload["stage_snapshots"] = [PipelineSnapshotIdentityPayload(stage) for stage in pipeline.stage_snapshots]
    return payload


def PipelineSnapshotIdentityPayload(stage: PipelineStageSnapshot) -> dict[str, _Json]:
    """Canonical aggregate audit fields; no unselected stock identities."""
    health = stage.source_health
    return {
        "stage": stage.stage.value,
        "stage_order": stage.stage_order,
        "input_batch_id": stage.input_batch_id,
        "output_batch_id": stage.output_batch_id,
        "as_of": stage.as_of.isoformat(),
        "state": stage.state.value,
        "input_count": stage.input_count,
        "output_count": stage.output_count,
        "rejected_count": stage.rejected_count,
        "pending_count": stage.pending_count,
        "failed_count": stage.failed_count,
        "not_selected_count": stage.not_selected_count,
        "reasons": [
            {"code": reason.code, "label": reason.label, "count": reason.count, "severity": reason.severity.value}
            for reason in stage.reasons
        ],
        "source_health": {
            "state": health.state.value,
            "source_count": health.source_count,
            "healthy_source_count": health.healthy_source_count,
            "latest_success_at": health.latest_success_at.isoformat() if health.latest_success_at is not None else None,
            "age_seconds": float(health.age_seconds) if health.age_seconds is not None else None,
        },
        **(
            {
                "operation_durations": [
                    {"operation": name, "duration_ms": duration} for name, duration in stage.operation_durations
                ]
            }
            if stage.operation_durations
            else {}
        ),
        "latency_ms": stage.latency_ms,
        "degraded": stage.degraded,
    }


def _historical_pipeline_payload(pipeline: HistoricalPipelineAudit) -> dict[str, _Json]:
    """Frozen persisted field whitelist; independent of the active pipeline encoder."""
    payload: dict[str, _Json] = {
        "current_stage": pipeline.current_stage,
        "stages": [
            {
                "key": stage.key,
                "state": stage.state,
                "input_count": stage.input_count,
                "output_count": stage.output_count,
                "metric_ranges": [
                    {"metric": item.metric, "minimum": item.minimum, "maximum": item.maximum}
                    for item in stage.metric_ranges
                ],
                "threshold": stage.threshold,
                "facets": [{"key": item.key, "count": item.count, "total": item.total} for item in stage.facets],
                "reason_counts": [{"reason": item.reason, "count": item.count} for item in stage.reason_counts],
            }
            for stage in pipeline.stages
        ],
    }
    if pipeline.elapsed_ms is not None:
        payload["elapsed_ms"] = pipeline.elapsed_ms
    if pipeline.stage_snapshots:
        payload["stage_snapshots"] = [_historical_snapshot_payload(stage) for stage in pipeline.stage_snapshots]
    return payload


def _historical_snapshot_payload(stage: HistoricalStageSnapshot) -> dict[str, _Json]:
    health = stage.source_health
    return {
        **({"not_selected_count": stage.not_selected_count} if stage.not_selected_count is not None else {}),
        "stage": stage.stage.value,
        "stage_order": stage.stage_order,
        "input_batch_id": stage.input_batch_id,
        "output_batch_id": stage.output_batch_id,
        "as_of": stage.as_of.isoformat(),
        "state": stage.state.value,
        "input_count": stage.input_count,
        "output_count": stage.output_count,
        "rejected_count": stage.rejected_count,
        "pending_count": stage.pending_count,
        "failed_count": stage.failed_count,
        "reasons": [
            {"code": item.code, "label": item.label, "count": item.count, "severity": item.severity.value}
            for item in stage.reasons
        ],
        "source_health": {
            "state": health.state.value,
            "source_count": health.source_count,
            "healthy_source_count": health.healthy_source_count,
            "latest_success_at": health.latest_success_at.isoformat() if health.latest_success_at is not None else None,
            "age_seconds": float(health.age_seconds) if health.age_seconds is not None else None,
        },
        **(
            {
                "operation_durations": [
                    {"operation": name, "duration_ms": duration} for name, duration in stage.operation_durations
                ]
            }
            if stage.operation_durations
            else {}
        ),
        "latency_ms": stage.latency_ms,
        "degraded": stage.degraded,
    }


def CommittedRecordIdentityPayload(record: CommittedFreezeRecord) -> dict[str, _Json]:
    """Return the explicit current-schema material shared by identity hashing and persistence."""

    if IsPreviewFreeze(record.freeze):
        raise ValueError("preview freezes cannot be encoded as formal records")
    return {
        "schema_version": record.schema_version,
        "freeze": _scored_payload(
            record.freeze,
            record.freeze.items,
            record.freeze.input_versions,
            record.freeze.filter_aggregates,
            record.freeze.degraded_reasons,
        ),
        "freeze_version": record.freeze.version,
        "freeze_hash": record.freeze.content_hash,
        "committed_at": record.committed_at.isoformat(),
        "commit_kind": record.commit_kind,
    }


def _freeze_item_payload(item: FreezeItem) -> dict[str, _Json]:
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
        "board": item.board.value,
        "selection_rank": item.selection_rank,
    }
    if item.score_ledger is not None:
        payload["score_ledger"] = ScoreLedgerPayload(item.score_ledger)
    if item.name:
        payload["name"] = item.name
    if item.industry:
        payload["industry"] = item.industry
    if item.quote is not None:
        payload["quote"] = _freeze_quote_payload(item.quote)
    payload["setup_type"] = item.setup_type
    payload["downside"] = _downside_payload(item.downside)
    payload["review_outcome"] = item.review_outcome
    payload["research_coverage"] = _research_coverage_payload(item.research_coverage)
    if item.risk_details:
        payload["risk_details"] = [
            {"risk_code": detail.risk_code, "origin": detail.origin, "veto_applied": detail.veto_applied}
            for detail in item.risk_details
        ]
    if item.model_diagnostics is not None:
        payload["model_diagnostics"] = _model_diagnostics_payload(item.model_diagnostics)
    return payload


def _downside_payload(value: FreezeDownside | None) -> dict[str, _Json] | None:
    if value is None:
        return None
    return {
        "status": value.status,
        "reasons": list(value.reasons),
        "atr20_pct": value.atr20_pct,
        "intraday_reversal_atr": value.intraday_reversal_atr,
        "historical_drawdown_pct": value.historical_drawdown_pct,
    }


def _research_coverage_payload(value: FreezeResearchCoverage | None) -> dict[str, _Json] | None:
    if value is None:
        return None
    payload: dict[str, _Json] = {
        "evidence_count": value.evidence_count,
        "structured_risk_fact_count": value.structured_risk_fact_count,
        "review_eligible": value.review_eligible,
    }
    if value.components is not None:
        payload["components"] = {
            "financial": value.components.financial,
            "announcements": value.components.announcements,
            "pledge": value.components.pledge,
            "unlock": value.components.unlock,
        }
    return payload


def _model_diagnostics_payload(value: FreezeModelDiagnostics) -> dict[str, _Json]:
    return {
        "signal_score": value.signal_score,
        "predicted_excess_return_pct": value.predicted_excess_return_pct,
        "estimated_cost_pct": value.estimated_cost_pct,
        "predicted_net_excess_pct": value.predicted_net_excess_pct,
        "model_disagreement_pct": value.model_disagreement_pct,
    }


def _selection_diagnostics_payload(value: SelectionDiagnostics | None) -> dict[str, _Json] | None:
    if value is None:
        return None
    payload: dict[str, _Json] = {
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
    if value.evaluated_count is not None:
        payload["evaluated_count"] = value.evaluated_count
    return payload


def _freeze_quote_payload(quote: FreezeQuote) -> dict[str, _Json]:
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


def _normalize_versions(values: tuple[tuple[str, str], ...]) -> tuple[tuple[str, str], ...]:
    versions = tuple(sorted(values))
    if not versions or len({name for name, _version in versions}) != len(versions):
        raise ValueError("input versions must be non-empty and unique")
    for name, version in versions:
        _require_identity(name, "input version name")
        _require_identity(version, "input version")
    return versions


def _normalize_counts(values: tuple[tuple[str, int], ...]) -> tuple[tuple[str, int], ...]:
    counts = tuple(sorted(values))
    if len({reason for reason, _count in counts}) != len(counts):
        raise ValueError("filter aggregates must contain unique reasons")
    if any(_REASON.fullmatch(reason) is None or count < 1 for reason, count in counts):
        raise ValueError("filter aggregates must use structured reasons and positive counts")
    return counts


def _normalize_reasons(values: tuple[str, ...]) -> tuple[str, ...]:
    reasons = tuple(sorted(set(values)))
    if any(_REASON.fullmatch(value) is None for value in reasons):
        raise ValueError("degraded reasons must be structured")
    return reasons


def _validate_coordinates(trade_date: date, sequence: int, observed_at: datetime) -> None:
    if sequence < 0:
        raise ValueError("identity sequence cannot be negative")
    _require_shanghai(observed_at, "identity observed_at")
    if observed_at.date() != trade_date:
        raise ValueError("identity observation must match its trade date")


def _validate_scored_schema(
    schema_version: str,
    diagnostics: SelectionDiagnostics | None,
    items: tuple[FreezeItem, ...],
) -> None:
    del diagnostics, items
    if schema_version != FREEZE_IDENTITY_SCHEMA_VERSION:
        raise ValueError(f"freeze schema_version must be {FREEZE_IDENTITY_SCHEMA_VERSION}")


def _validate_score(value: float, label: str) -> None:
    if not math.isfinite(value) or not 0.0 <= value <= 100.0:
        raise ValueError(f"{label} must be finite and in [0, 100]")


def _validate_freeze_item_selection(item: FreezeItem) -> None:
    if item.selected and item.rank < 1:
        raise ValueError("selected freezes require a positive fixed-seat rank")
    if not item.selected and item.rank != 0:
        raise ValueError("unselected freezes must use rank zero")
    if not isinstance(item.board, Board):
        raise ValueError("freeze item board is invalid")
    if not item.selected and item.action is RecommendationAction.UNAVAILABLE and item.selection_rank != 0:
        raise ValueError("unavailable freezes must use selection rank zero")
    if item.action is not RecommendationAction.UNAVAILABLE and item.selection_rank < 1:
        raise ValueError("available freezes require a positive selection rank")


def _validate_optional_score(value: float | None, label: str) -> None:
    if value is not None:
        _validate_score(value, label)


def _validate_optional_market_value(
    value: float | None,
    label: str,
    *,
    positive: bool = False,
    non_negative: bool = False,
) -> None:
    if value is None:
        return
    if not math.isfinite(value):
        raise ValueError(f"{label} must be finite")
    if positive and value <= 0.0:
        raise ValueError(f"{label} must be positive")
    if non_negative and value < 0.0:
        raise ValueError(f"{label} cannot be negative")


def _require_code(value: str) -> None:
    if _CODE.fullmatch(value) is None:
        raise ValueError("stock code must contain six digits")


def _require_identity(value: str, label: str) -> None:
    if _IDENTITY.fullmatch(value) is None:
        raise ValueError(f"{label} is invalid")


def _require_shanghai(value: datetime, label: str) -> None:
    if (
        value.tzinfo is None
        or value.utcoffset() is None
        or not (isinstance(value.tzinfo, ZoneInfo) and value.tzinfo.key == _SHANGHAI.key)
    ):
        raise ValueError(f"{label} must use Asia/Shanghai")


def _hash(payload: dict[str, _Json]) -> str:
    return CanonicalDigest(payload)


def _display_text(value: str, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be text")
    normalized = value.strip()
    if len(normalized) > 120 or any(ord(character) < 32 for character in normalized):
        raise ValueError(f"{label} is invalid")
    return normalized


def _validate_coverage_counts(population_count: int | None, rejected_count: int | None, item_count: int) -> None:
    if (population_count is None) != (rejected_count is None):
        raise ValueError("freeze coverage counts must be provided together")
    if population_count is None or rejected_count is None:
        return
    if population_count < item_count or rejected_count < 0 or rejected_count > population_count:
        raise ValueError("freeze coverage counts are invalid")


__all__ = [
    "IsShadowFreeze",
    "IsPreviewFreeze",
    "COMMITTED_RECORD_SCHEMA_VERSION",
    "FREEZE_IDENTITY_SCHEMA_VERSION",
    "LONG_PROJECTION_SCHEMA_VERSION",
    "OVERLAY_SCHEMA_VERSION",
    "CommitKind",
    "CommittedFreezeRecord",
    "FreezeIdentity",
    "FreezeItem",
    "FreezeModelDiagnostics",
    "FreezeDownside",
    "FreezeOverlay",
    "FreezeQuote",
    "FreezeResearchCoverage",
    "FreezeStage",
    "LongProjection",
    "LongProjectionItem",
    "ScoredFreeze",
    "SelectionDiagnostics",
    "CommittedRecordIdentityPayload",
    "FormalScoredFreeze",
    "DisplayQuoteCodes",
    "IdentityCodes",
    "VisibleScoredSeats",
]
