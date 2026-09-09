"""Build a production-isolated, replayable Tomorrow point-in-time dataset."""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Protocol, cast
from zoneinfo import ZoneInfo

from trader.domain.market.feature_contracts import FeatureValue, FeatureVectorManifest
from trader.domain.market.models import FeatureSnapshot
from trader.domain.outcome.evaluation import CanonicalOutcomeEvaluator, OutcomeEvaluationRequest
from trader.domain.outcome.models import BenchmarkConstituentReturn, OutcomeBar, OutcomeTarget, RecommendationOutcome
from trader.domain.recommendation.models import ScoredStockEvaluation, Strategy
from trader.domain.recommendation.selection.scored_selection import (
    ScoredSelectionPolicy,
    ScoredSelectionRequest,
    select_scored,
)
from trader.domain.research.h1_point_in_time import canonical_hash
from trader.domain.research.point_in_time_data_qualification import PointInTimeDataQualificationReport
from trader.domain.research.point_in_time_dataset import (
    POINT_IN_TIME_BOUNDARIES,
    POINT_IN_TIME_COST_BPS,
    PointInTimeBoardPopulation,
    PointInTimeBoundaryCount,
    PointInTimeCostOutcome,
    PointInTimeCoverage,
    PointInTimeDatasetManifest,
    PointInTimeDatasetReport,
    PointInTimeDatasetRow,
    PointInTimeDateSplit,
    PointInTimeDayDataset,
    PointInTimeEventFact,
    PointInTimeIndustryFact,
    PointInTimePartitionManifest,
    PointInTimePartitionName,
    PointInTimeRejectionBoundary,
    PointInTimeSourceIdentity,
)
from trader.domain.review.models import RiskRule

_HASH = re.compile(r"^[0-9a-f]{64}$")
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_PERMANENT_REASONS = frozenset(
    {
        "st_or_delisting",
        "blacklisted",
        "financial_fraud_history",
        "major_illegal_history",
        "fund_occupation_history",
        "illegal_guarantee_history",
        "forced_delisting_risk",
    }
)
_FIELD_REASONS = frozenset(
    {
        "candidate_core_missing",
        "strategy_history_insufficient",
        "production_model_features_missing",
    }
)


class _PointInTimeDatasetIncompleteError(RuntimeError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class _DayBuildContext:
    source: PointInTimeDaySource
    feature_manifest: FeatureVectorManifest
    benchmark_return: float


@dataclass(frozen=True)
class PointInTimeSourceRow:
    feature: FeatureSnapshot
    anchor_raw_price: float
    atr20_pct: float
    outcome_bars: tuple[OutcomeBar, ...]
    expected_trade_dates: tuple[str, ...]
    settled_at: datetime
    source_identity: PointInTimeSourceIdentity
    industry_fact: PointInTimeIndustryFact
    event_facts: tuple[PointInTimeEventFact, ...]

    def __post_init__(self) -> None:
        bars = tuple(sorted(self.outcome_bars, key=lambda item: item.trade_date))
        dates = tuple(self.expected_trade_dates)
        facts = tuple(self.event_facts)
        if len(self.feature.quote.code) != 6 or not self.feature.quote.code.isdigit():
            raise ValueError("point-in-time source row code is invalid")
        if not math.isfinite(self.anchor_raw_price) or self.anchor_raw_price <= 0.0:
            raise ValueError("point-in-time source raw anchor is invalid")
        if self.feature.quote.price is None or not math.isclose(self.feature.quote.price, self.anchor_raw_price):
            raise ValueError("point-in-time source raw anchor does not match the quote")
        if not math.isfinite(self.atr20_pct) or self.atr20_pct <= 0.0:
            raise ValueError("point-in-time source ATR20 is invalid")
        if (
            len({item.trade_date for item in bars}) != len(bars)
            or len({item.source for item in bars}) != 1
            or len(dates) != 1
        ):
            raise ValueError("point-in-time source outcome window is invalid")
        if tuple(sorted(set(dates))) != dates:
            raise ValueError("point-in-time expected dates must be sorted and unique")
        _require_shanghai(self.settled_at, "point-in-time settlement")
        object.__setattr__(self, "outcome_bars", bars)
        object.__setattr__(self, "expected_trade_dates", dates)
        object.__setattr__(self, "event_facts", facts)


@dataclass(frozen=True)
class PointInTimeDaySource:
    trade_date: date
    anchor_at: datetime
    rows: tuple[PointInTimeSourceRow, ...]

    def __post_init__(self) -> None:
        _require_shanghai(self.anchor_at, "point-in-time source anchor")
        local = self.anchor_at.astimezone(_SHANGHAI)
        rows = tuple(sorted(self.rows, key=lambda item: item.feature.quote.code))
        if local.date() != self.trade_date or local.timetz().replace(tzinfo=None) != time(14, 50):
            raise ValueError("point-in-time source requires the Tomorrow 14:50 anchor")
        if not rows or len({item.feature.quote.code for item in rows}) != len(rows):
            raise ValueError("point-in-time source day requires unique rows")
        for row in rows:
            if row.feature.observed_at > self.anchor_at:
                raise ValueError("point-in-time source contains future features")
            if any(fact.anchor_at != self.anchor_at for fact in row.event_facts):
                raise ValueError("point-in-time source event anchor does not match the day")
            if row.industry_fact.code != row.feature.quote.code:
                raise ValueError("point-in-time source industry fact does not match the row")
            if not any(bar.trade_date == self.trade_date.isoformat() for bar in row.outcome_bars):
                raise ValueError("point-in-time source is missing the raw/qfq reference bar")
            expected = date.fromisoformat(row.expected_trade_dates[0])
            if (
                expected <= self.trade_date
                or any(date.fromisoformat(bar.trade_date) < self.trade_date for bar in row.outcome_bars)
                or row.settled_at <= self.anchor_at
            ):
                raise ValueError("point-in-time source outcome dates are invalid")
        object.__setattr__(self, "rows", rows)


class PointInTimeDatasetSourcePort(Protocol):
    def load_day(self, trade_date: date) -> PointInTimeDaySource: ...


@dataclass(frozen=True)
class PointInTimeDatasetBuildRequest:
    qualification: PointInTimeDataQualificationReport
    date_split: PointInTimeDateSplit
    feature_manifest: FeatureVectorManifest
    calendar_hash: str
    security_master_hash: str
    selection_policy: ScoredSelectionPolicy
    selection_policy_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if any(_HASH.fullmatch(value) is None for value in (self.calendar_hash, self.security_master_hash)):
            raise ValueError("point-in-time dataset build identity is invalid")
        if self.selection_policy.strategy is not Strategy.TOMORROW:
            raise ValueError("point-in-time dataset requires the Tomorrow selection policy")
        object.__setattr__(self, "selection_policy_hash", _selection_policy_hash(self.selection_policy))


class PointInTimeDatasetBuilder:
    def __init__(
        self,
        source: PointInTimeDatasetSourcePort,
        evaluator: CanonicalOutcomeEvaluator | None = None,
    ) -> None:
        self._source = source
        self._evaluator = evaluator or CanonicalOutcomeEvaluator()

    def build(self, request: PointInTimeDatasetBuildRequest) -> PointInTimeDatasetReport:
        qualification = request.qualification
        if qualification.state != "qualified" or not qualification.point_in_time_parity:
            return PointInTimeDatasetReport(
                qualification_hash=qualification.content_hash,
                state="historical_data_insufficient",
                days=(),
                manifest=None,
                failure_reasons=("point_in_time_data_not_qualified",),
            )
        try:
            days = tuple(
                self._build_day(self._source.load_day(item), request) for item in request.date_split.development_dates
            )
        except _PointInTimeDatasetIncompleteError as exc:
            return PointInTimeDatasetReport(
                qualification_hash=qualification.content_hash,
                state="historical_data_insufficient",
                days=(),
                manifest=None,
                failure_reasons=(exc.reason,),
            )
        if any(not row.label_complete for day in days for row in day.rows):
            return PointInTimeDatasetReport(
                qualification_hash=qualification.content_hash,
                state="historical_data_insufficient",
                days=(),
                manifest=None,
                failure_reasons=("outcome_label_incomplete",),
            )
        partitions = (
            self._partition("training", request.date_split.training_dates, days),
            self._partition("early_stopping", request.date_split.early_stopping_dates, days),
            self._partition("calibration", request.date_split.calibration_dates, days),
            self._partition("confirmation", request.date_split.confirmation_dates, days),
        )
        manifest = PointInTimeDatasetManifest(
            qualification_hash=qualification.content_hash,
            daily_archive_manifest_hash=qualification.daily_archive.manifest_hash,
            feature_manifest_hash=request.feature_manifest.content_hash,
            calendar_hash=request.calendar_hash,
            security_master_hash=request.security_master_hash,
            selection_policy_hash=request.selection_policy_hash,
            date_split=request.date_split,
            date_split_hash=request.date_split.content_hash,
            partitions=partitions,
            total_rows=sum(len(day.rows) for day in days),
        )
        return PointInTimeDatasetReport(
            qualification_hash=qualification.content_hash,
            state="historical_point_in_time_parity",
            days=days,
            manifest=manifest,
            failure_reasons=(),
        )

    def _build_day(
        self,
        source: PointInTimeDaySource,
        request: PointInTimeDatasetBuildRequest,
    ) -> PointInTimeDayDataset:
        selection = select_scored(
            ScoredSelectionRequest(
                features=tuple(item.feature for item in source.rows),
                evaluated_at=source.anchor_at,
                trade_date=source.trade_date.isoformat(),
                phase="final_quote",
                data_version=f"point-in-time:{source.trade_date.isoformat()}",
                merge_epoch=f"point-in-time:{canonical_hash(tuple(item.source_identity for item in source.rows))}",
                policy=request.selection_policy,
            )
        )
        evaluations = {item.code: item for item in selection.evaluations}
        boundaries = {code: _first_boundary(item) for code, item in evaluations.items()}
        preliminary = {item.feature.quote.code: self._evaluate(item, source.trade_date, (), 20) for item in source.rows}
        constituents = tuple(
            BenchmarkConstituentReturn(code, source.trade_date.isoformat(), outcome.gross_return_pct)
            for code, outcome in preliminary.items()
            if boundaries[code][0] in {"eligible", "candidate_threshold", "board_limit"}
            and outcome.gross_return_pct is not None
        )
        expected_benchmark_count = sum(
            boundary in {"eligible", "candidate_threshold", "board_limit"} for boundary, _reasons in boundaries.values()
        )
        if len(constituents) != expected_benchmark_count:
            raise _PointInTimeDatasetIncompleteError("benchmark_population_outcome_incomplete")
        benchmark = self._evaluator.equal_weight_benchmark(source.trade_date.isoformat(), constituents)
        if benchmark is None:
            raise _PointInTimeDatasetIncompleteError("benchmark_population_unavailable")
        context = _DayBuildContext(source, request.feature_manifest, benchmark.return_pct)
        rows = tuple(
            self._build_row(
                item,
                evaluations[item.feature.quote.code],
                boundaries[item.feature.quote.code],
                context,
            )
            for item in source.rows
        )
        counts = Counter(item.first_rejection_boundary for item in rows)
        coverage = PointInTimeCoverage(
            total_rows=len(rows),
            benchmark_eligible_rows=sum(item.benchmark_eligible for item in rows),
            candidate_eligible_rows=sum(item.candidate_eligible for item in rows),
            label_complete_rows=sum(item.label_complete for item in rows),
            boundary_counts=tuple(
                PointInTimeBoundaryCount(boundary, counts[boundary]) for boundary in POINT_IN_TIME_BOUNDARIES
            ),
        )
        board_populations = tuple(
            PointInTimeBoardPopulation(
                board,
                version,
                sum(item.benchmark_eligible and item.board is board for item in rows),
            )
            for board, version in sorted(selection.population_versions.items(), key=lambda item: item[0].value)
            if any(item.benchmark_eligible and item.board is board for item in rows)
        )
        return PointInTimeDayDataset(source.trade_date, source.anchor_at, rows, coverage, board_populations)

    @staticmethod
    def _partition(
        name: str,
        dates: tuple[date, ...],
        days: tuple[PointInTimeDayDataset, ...],
    ) -> PointInTimePartitionManifest:
        by_date = {item.trade_date: item for item in days}
        selected = tuple(by_date[item] for item in dates)
        return PointInTimePartitionManifest(
            cast(PointInTimePartitionName, name),
            dates,
            tuple(item.content_hash for item in selected),
            sum(len(item.rows) for item in selected),
        )

    def _build_row(
        self,
        source_row: PointInTimeSourceRow,
        evaluation: ScoredStockEvaluation,
        boundary: tuple[PointInTimeRejectionBoundary, tuple[str, ...]],
        context: _DayBuildContext,
    ) -> PointInTimeDatasetRow:
        first_boundary, reasons = boundary
        feature_vector = context.feature_manifest.bind(
            tuple(
                FeatureValue(feature_id, source_row.feature.optional_value(feature_id.value))
                for feature_id in context.feature_manifest.feature_ids
            )
        )
        outcomes = tuple(
            PointInTimeCostOutcome(
                cost_bps,
                self._evaluate(source_row, context.source.trade_date, (context.benchmark_return,), cost_bps),
            )
            for cost_bps in POINT_IN_TIME_COST_BPS
        )
        return PointInTimeDatasetRow(
            code=source_row.feature.quote.code,
            trade_date=context.source.trade_date,
            anchor_at=context.source.anchor_at,
            board=evaluation.features.quote.board,
            industry=evaluation.features.quote.industry,
            anchor_raw_price=source_row.anchor_raw_price,
            feature_vector=feature_vector,
            source_identity=source_row.source_identity,
            industry_fact=source_row.industry_fact,
            event_facts=source_row.event_facts,
            first_rejection_boundary=first_boundary,
            rejection_reasons=reasons,
            benchmark_eligible=first_boundary in {"eligible", "candidate_threshold", "board_limit"},
            candidate_eligible=first_boundary == "eligible",
            candidate_score=evaluation.candidate_score,
            candidate_rank=evaluation.candidate_audit_rank,
            outcomes=outcomes,
        )

    def _evaluate(
        self,
        row: PointInTimeSourceRow,
        trade_date: date,
        benchmark_returns: tuple[float, ...],
        cost_bps: int,
    ) -> RecommendationOutcome:
        return self._evaluator.evaluate(
            OutcomeEvaluationRequest(
                target=OutcomeTarget(
                    snapshot_id=f"point-in-time:{trade_date.isoformat()}:{row.feature.quote.code}",
                    strategy=Strategy.TOMORROW,
                    recommend_date=trade_date.isoformat(),
                    stock_code=row.feature.quote.code,
                    anchor_raw_price=row.anchor_raw_price,
                    atr20_pct=row.atr20_pct,
                ),
                bars=row.outcome_bars,
                horizon=1,
                benchmark_returns=benchmark_returns,
                settled_at=row.settled_at,
                expected_trade_dates=row.expected_trade_dates,
                round_trip_cost_pct=cost_bps / 100.0,
            )
        )


def _first_boundary(
    evaluation: ScoredStockEvaluation,
) -> tuple[PointInTimeRejectionBoundary, tuple[str, ...]]:
    hard_reasons = tuple(item.code for item in evaluation.filter_reasons)
    if hard_reasons:
        boundary: PointInTimeRejectionBoundary = (
            "permanent_eligibility" if hard_reasons[0] in _PERMANENT_REASONS else "dynamic_hard_filter"
        )
        return boundary, hard_reasons
    if evaluation.selection_skip_reason in _FIELD_REASONS:
        return "field_eligibility", (evaluation.selection_skip_reason,)
    pruning = evaluation.candidate_audit_pruning_reason or evaluation.selection_skip_reason
    if pruning in _FIELD_REASONS:
        return "field_eligibility", (pruning,)
    if pruning == "candidate_score_below_minimum":
        return "candidate_threshold", (pruning,)
    if pruning == "board_candidate_limit":
        return "board_limit", (pruning,)
    return "eligible", ()


def _require_shanghai(value: datetime, owner: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None or getattr(value.tzinfo, "key", None) != "Asia/Shanghai":
        raise ValueError(f"{owner} must use Asia/Shanghai")


def _selection_policy_hash(policy: ScoredSelectionPolicy) -> str:
    board_policies = tuple(
        (
            board.value,
            value.policy_id,
            value.version,
            tuple(sorted(value.candidate_weights.items())),
            tuple(sorted(value.local_weights.items())),
            value.candidate_min_score,
            value.minimum_reliability,
        )
        for board, value in sorted(policy.board_policies.items(), key=lambda item: item[0].value)
    )
    risk_rules = tuple((name, _risk_rule_identity(value)) for name, value in sorted(policy.risk_rules.items()))
    return canonical_hash(
        (
            "point_in_time_selection_policy",
            board_policies,
            risk_rules,
            policy.max_age_seconds,
            policy.local_risk_cap,
            policy.candidate_limit_per_board,
            policy.top_k,
            policy.maximum_per_industry,
            policy.minimum_local_score,
            tuple(sorted(policy.hard_filter.blacklist_codes)),
            tuple(sorted(policy.hard_filter.structured_risk_thresholds.items())),
            policy.strategy.value,
        )
    )


def _risk_rule_identity(rule: RiskRule) -> tuple[object, ...]:
    return (
        rule.risk_code,
        rule.severity,
        rule.penalty,
        rule.minimum_confidence,
        rule.group,
        rule.evidence_ttl_hours,
        rule.veto,
        rule.allowed_evidence_types,
        rule.strategies,
        rule.trigger_factor,
        rule.trigger_operator,
        rule.trigger_thresholds,
        rule.combination_mode,
        rule.risk_fact_id_fields,
        rule.local_trigger_enabled,
    )


__all__ = [
    "PointInTimeDatasetBuildRequest",
    "PointInTimeDatasetBuilder",
    "PointInTimeDatasetSourcePort",
    "PointInTimeDaySource",
    "PointInTimeSourceRow",
]
