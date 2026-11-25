"""Bounded external evidence for the paired diagnostic company replay."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
from typing import TYPE_CHECKING, cast
from zoneinfo import ZoneInfo

from trader.infra.regular_file import OpenRegularFile
from trader.infra.settings.parser import Boolean, Integer, Number, RequireExactKeys, RequireMapping, Text
from trader.recommendation.domain.outcomes.prediction_target import PredictionTarget
from trader.recommendation.domain.publication.models import RecommendationAction, ScoredDisposition, Strategy
from trader.recommendation.domain.scoring.profile_identity import SCORING_PROFILE_IDS, ScoringProfileId
from trader.recommendation.domain.selection.scored_selection import SplitFilterReasonCounts
from trader.training.application.company_replay import CompanyReplayEvidenceKind, CompanyReplayReadiness
from trader.training.application.terminal_holdout_execution import ResearchCancelledError
from trader.training.domain.evaluation.company_replay import (
    COMPANY_REPLAY_ARMS,
    CompanyReplayArm,
    CompanyReplayDay,
    CompanyReplayOutcome,
    CompanyReplayRejection,
    CompanyReplaySelection,
)

if TYPE_CHECKING:
    from trader.recommendation.application.pipeline.final_selection.company_replay import CompanyReplayResult

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_CLOSE = time(15, 0)
_MAX_REPLAY_DATES = 1000
_MAX_LINE_BYTES = 64 * 1024
_SHA256 = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class _Plan:
    evidence_kind: CompanyReplayEvidenceKind
    profile: ScoringProfileId
    target: PredictionTarget
    trade_dates: tuple[date, ...]
    maturity_dates: tuple[date, ...]
    qualification_sha256: str
    replays_sha256: str
    outcomes_sha256: str


@dataclass(frozen=True)
class _Replay:
    trade_date: date
    signal_identity: str
    selections: tuple[CompanyReplaySelection, ...]


class CompanyReplayEvidenceReader:
    def __init__(self, root: Path, *, cancel_requested: Callable[[], bool]) -> None:
        self._root = root
        self._cancel_requested = cancel_requested

    def inspect(self) -> CompanyReplayReadiness:
        try:
            content = self._read_document("plan.json")
        except FileNotFoundError:
            return CompanyReplayReadiness(None, None, None, ("company_replay_plan_unavailable",))
        plan = _plan(_object(json.loads(content)))
        try:
            qualification = self._read_document("qualification.json")
        except FileNotFoundError:
            return CompanyReplayReadiness(
                plan.evidence_kind, plan.profile, plan.target, ("point_in_time_qualification_unavailable",),
            )
        if hashlib.sha256(qualification).hexdigest() != plan.qualification_sha256:
            raise ValueError("company replay qualification digest mismatch")
        receipt = _object(json.loads(qualification))
        if Boolean(receipt, "terminal_holdout_opened") or Boolean(receipt, "production_authority"):
            raise ValueError("company replay requires development evidence")
        status = Text(receipt, "status")
        parity = Boolean(receipt, "point_in_time_parity")
        if status not in {"qualified", "historical_data_insufficient"} or parity != (status == "qualified"):
            raise ValueError("company replay qualification status is invalid")
        reasons = _text_items(receipt, "failure_reasons")
        if parity == bool(reasons):
            raise ValueError("company replay qualification reasons are inconsistent")
        if plan.evidence_kind == "point_in_time":
            reasons = (*reasons, "independent_point_in_time_replay_proof_unavailable")
        return CompanyReplayReadiness(
            plan.evidence_kind,
            plan.profile,
            plan.target,
            reasons,
            hashlib.sha256(content).hexdigest(),
        )

    def read_mature_days(
        self, *, readiness: CompanyReplayReadiness, evaluation_at: datetime,
    ) -> tuple[CompanyReplayDay, ...] | None:
        content = self._read_document("plan.json")
        if (
            readiness.plan_sha256 is None
            or readiness.failure_reasons
            or hashlib.sha256(content).hexdigest() != readiness.plan_sha256
        ):
            raise ValueError("company replay plan differs from admitted evidence")
        plan = _plan(_object(json.loads(content)))
        if plan.evidence_kind != "fixture":
            return None
        if evaluation_at.utcoffset() is None:
            raise ValueError("company replay evaluation time must be timezone-aware")
        if evaluation_at < _close_at(plan.maturity_dates[-1]):
            return None
        try:
            replays = tuple(
                _replay(row, plan)
                for row in self._read_lines("replays.jsonl", plan.replays_sha256, len(plan.trade_dates))
            )
        except FileNotFoundError:
            return None
        if tuple(row.trade_date for row in replays) != plan.trade_dates:
            raise ValueError("company replay dates differ from their sealed plan")
        try:
            outcomes = self._read_lines("outcomes.jsonl", plan.outcomes_sha256, len(plan.trade_dates))
        except FileNotFoundError:
            return None
        return tuple(
            _mature_day(replay, row, maturity, plan.target, evaluation_at)
            for replay, row, maturity in zip(replays, outcomes, plan.maturity_dates, strict=True)
        )

    def _check(self) -> None:
        if self._cancel_requested():
            raise ResearchCancelledError("research_cancelled")

    def _read_document(self, name: str) -> bytes:
        limit = 1024 * 1024
        with OpenRegularFile(self._root / name, maximum_bytes=limit, check=self._check) as stream:
            return stream.read()

    def _read_lines(self, name: str, expected_sha256: str, days: int) -> tuple[Mapping[str, object], ...]:
        digest = hashlib.sha256()
        rows: list[Mapping[str, object]] = []
        with OpenRegularFile(self._root / name, maximum_bytes=_MAX_LINE_BYTES * days, check=self._check) as stream:
            while line := stream.readline(_MAX_LINE_BYTES + 1):
                if len(line) > _MAX_LINE_BYTES or len(rows) >= days:
                    raise ValueError("company replay evidence exceeds its declared bounds")
                digest.update(line)
                rows.append(_object(json.loads(line)))
        if len(rows) != days or digest.hexdigest() != expected_sha256:
            raise ValueError("company replay evidence differs from its sealed plan")
        return tuple(rows)


def ProjectCompanyReplayDay(result: CompanyReplayResult) -> dict[str, object]:
    """Project native local decisions into one offline review record."""
    fixed_codes = frozenset(seat.code for arm in result.arms for seat in arm.seats)
    return {
        "trade_date": result.trade_date.isoformat(),
        "evaluated_at": result.evaluated_at.isoformat(),
        "profile": result.profile,
        "strategy": result.strategy.value,
        "signal_identity": result.signal_identity,
        "config_identity": result.config_identity,
        "model_identity": result.model_identity,
        "prediction_target_bps": result.prediction_target_bps,
        "current_company": _project_publication(result.current_company.identity, result.current_company.published_at),
        "candidate_company": _project_publication(result.candidate_company.identity, result.candidate_company.published_at),
        "arms": [
            {
                "arm": arm.name,
                "cost_reference": arm.decision.cost_reference,
                "fixed": [seat.code for seat in arm.seats],
                "executable": [seat.code for seat in arm.seats if seat.action is RecommendationAction.EXECUTABLE],
                "rejections": [
                    {"code": row.code, "reasons": list(reasons)}
                    for row in arm.decision.rows
                    if row.code in fixed_codes
                    and row.disposition is ScoredDisposition.REJECT
                    and (reasons := _business_reasons(row.filter_reasons))
                ],
            }
            for arm in result.arms
        ],
    }


def _business_reasons(reasons: tuple[str, ...]) -> tuple[str, ...]:
    business, _readiness = SplitFilterReasonCounts(dict.fromkeys(reasons, 1))
    return tuple(sorted(business))


def _project_publication(identity: str, published_at: datetime) -> dict[str, str]:
    return {"identity": identity, "published_at": published_at.isoformat()}


def _plan(raw: Mapping[str, object]) -> _Plan:
    RequireExactKeys(
        raw,
        {
            "evidence_kind", "profile", "strategy", "prediction_target_bps", "trade_dates", "calendar_dates",
            "qualification_sha256", "replays_sha256", "outcomes_sha256",
        },
        "company_replay_plan",
    )
    kind = Text(raw, "evidence_kind")
    profile = Text(raw, "profile")
    if kind not in {"fixture", "point_in_time"} or profile not in SCORING_PROFILE_IDS:
        raise ValueError("company replay evidence kind or profile is invalid")
    target = PredictionTarget(Strategy(Text(raw, "strategy")), Integer(raw, "prediction_target_bps", minimum=0))
    trade_dates = _dates(raw, "trade_dates")
    calendar = _dates(raw, "calendar_dates")
    if len(trade_dates) > _MAX_REPLAY_DATES:
        raise ValueError("company replay exceeds the bounded date count")
    offsets = {day: index for index, day in enumerate(calendar)}
    horizon = 1 if target.strategy is Strategy.TOMORROW else 5
    maturity = []
    for day in trade_dates:
        index = offsets.get(day)
        if index is None or index + horizon >= len(calendar):
            raise ValueError("company replay dates require all maturity sessions in the sealed trading calendar")
        maturity.append(calendar[index + horizon])
    return _Plan(
        cast(CompanyReplayEvidenceKind, kind),
        profile,
        target,
        trade_dates,
        tuple(maturity),
        _digest(raw, "qualification_sha256"),
        _digest(raw, "replays_sha256"),
        _digest(raw, "outcomes_sha256"),
    )


def _replay(raw: Mapping[str, object], plan: _Plan) -> _Replay:
    RequireExactKeys(
        raw,
        {
            "trade_date", "evaluated_at", "profile", "strategy", "signal_identity", "config_identity",
            "model_identity", "prediction_target_bps", "current_company", "candidate_company", "arms",
        },
        "company_replay_day",
    )
    trade_date = date.fromisoformat(Text(raw, "trade_date"))
    evaluated_at = _timestamp(raw, "evaluated_at")
    current = _publication(RequireMapping(raw, "current_company"))
    candidate = _publication(RequireMapping(raw, "candidate_company"))
    if (
        Text(raw, "profile") != plan.profile
        or Text(raw, "strategy") != plan.target.strategy.value
        or Integer(raw, "prediction_target_bps", minimum=0) != plan.target.threshold_bps
        or evaluated_at != _close_at(trade_date)
        or not current <= candidate <= evaluated_at
    ):
        raise ValueError("company replay decision is outside its frozen scope or original cutoff")
    Text(raw, "model_identity")
    Text(raw, "config_identity")
    selections = tuple(_selection(_object(item)) for item in _items(raw, "arms"))
    if tuple(item.arm for item in selections) != COMPANY_REPLAY_ARMS:
        raise ValueError("company replay requires four ordered arms")
    baseline, risk, financial, combined = selections
    if baseline.cost_reference != financial.cost_reference or risk.cost_reference != combined.cost_reference:
        raise ValueError("company replay financial overlay changed its cost reference")
    return _Replay(trade_date, Text(raw, "signal_identity"), selections)


def _selection(raw: Mapping[str, object]) -> CompanyReplaySelection:
    RequireExactKeys(raw, {"arm", "cost_reference", "fixed", "executable", "rejections"}, "company_replay_selection")
    reference = Text(raw, "cost_reference") if raw["cost_reference"] is not None else None
    return CompanyReplaySelection(
        cast(CompanyReplayArm, Text(raw, "arm")),
        reference,
        _text_items(raw, "fixed"),
        _text_items(raw, "executable"),
        tuple(_rejection(_object(item)) for item in _items(raw, "rejections")),
    )


def _rejection(raw: Mapping[str, object]) -> CompanyReplayRejection:
    RequireExactKeys(raw, {"code", "reasons"}, "company_replay_rejection")
    reasons = _text_items(raw, "reasons")
    if reasons != _business_reasons(reasons):
        raise ValueError("company replay business rejection reason is invalid")
    return CompanyReplayRejection(Text(raw, "code"), reasons)


def _mature_day(
    replay: _Replay,
    raw: Mapping[str, object],
    maturity: date,
    target: PredictionTarget,
    evaluation_at: datetime,
) -> CompanyReplayDay:
    RequireExactKeys(
        raw,
        {"trade_date", "signal_identity", "settled_at", "maturity_date", "prediction_target_bps", "outcomes"},
        "company_replay_settlement",
    )
    if (
        date.fromisoformat(Text(raw, "trade_date")) != replay.trade_date
        or Text(raw, "signal_identity") != replay.signal_identity
        or Integer(raw, "prediction_target_bps", minimum=0) != target.threshold_bps
        or date.fromisoformat(Text(raw, "maturity_date")) != maturity
        or not _close_at(maturity) <= _timestamp(raw, "settled_at") <= evaluation_at
    ):
        raise ValueError("company replay settlement does not match the sealed signal and maturity")
    horizon_count = 1 if target.strategy is Strategy.TOMORROW else 4
    return CompanyReplayDay(
        replay.trade_date, replay.selections, tuple(_outcome(_object(item), horizon_count) for item in _items(raw, "outcomes")),
    )


def _outcome(raw: Mapping[str, object], horizon_count: int) -> CompanyReplayOutcome:
    RequireExactKeys(
        raw,
        {"code", "hit", "entry_tradable", "exit_untradable", "severe_loss", "pre_cost_excess_returns"},
        "company_replay_outcome",
    )
    returns = None
    if raw["pre_cost_excess_returns"] is not None:
        items = _items(raw, "pre_cost_excess_returns")
        if len(items) != horizon_count:
            raise ValueError("company replay outcome does not contain all expected horizons")
        returns = tuple(Number({"return": item}, "return") for item in items)
    return CompanyReplayOutcome(
        code=Text(raw, "code"),
        hit=_optional_flag(raw, "hit"),
        entry_tradable=_optional_flag(raw, "entry_tradable"),
        exit_untradable=_optional_flag(raw, "exit_untradable"),
        severe_loss=_optional_flag(raw, "severe_loss"),
        pre_cost_excess_returns=returns,
    )


def _publication(raw: Mapping[str, object]) -> datetime:
    RequireExactKeys(raw, {"identity", "published_at"}, "company_replay_publication")
    Text(raw, "identity")
    return _timestamp(raw, "published_at")


def _close_at(day: date) -> datetime:
    return datetime.combine(day, _CLOSE, _SHANGHAI)


def _timestamp(raw: Mapping[str, object], key: str) -> datetime:
    timestamp = datetime.fromisoformat(Text(raw, key))
    if timestamp.utcoffset() is None:
        raise ValueError("company replay timestamps must be timezone-aware")
    return timestamp


def _dates(raw: Mapping[str, object], key: str) -> tuple[date, ...]:
    values = tuple(date.fromisoformat(value) for value in _text_items(raw, key))
    if not values or values != tuple(sorted(set(values))):
        raise ValueError("company replay calendar and signal dates must be nonempty, sorted, and unique")
    return values


def _digest(raw: Mapping[str, object], key: str) -> str:
    value = Text(raw, key)
    if _SHA256.fullmatch(value) is None:
        raise ValueError("company replay evidence requires a SHA-256 digest")
    return value


def _optional_flag(raw: Mapping[str, object], key: str) -> bool | None:
    return None if raw[key] is None else Boolean(raw, key)


def _items(raw: Mapping[str, object], key: str) -> tuple[object, ...]:
    values = raw.get(key)
    if not isinstance(values, list):
        raise ValueError("company replay evidence requires array fields")
    return tuple(values)


def _text_items(raw: Mapping[str, object], key: str) -> tuple[str, ...]:
    return tuple(Text({"value": value}, "value") for value in _items(raw, key))


def _object(raw: object) -> Mapping[str, object]:
    if not isinstance(raw, dict) or any(not isinstance(key, str) for key in raw):
        raise ValueError("company replay evidence requires JSON objects")
    return raw


__all__ = [
    "CompanyReplayEvidenceReader",
    "ProjectCompanyReplayDay",
]
