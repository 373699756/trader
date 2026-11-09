"""Bounded external terminal inputs with a durable, first-writer opening record."""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Callable, Iterator, Mapping
from contextlib import ExitStack, contextmanager
from datetime import date
from pathlib import Path

from trader.infra.serialization.sealing import PublishImmutable
from trader.infra.regular_file import OpenRegularFile, ReadRegularFile, RegularFileStream
from trader.infra.settings.parser import Boolean, Integer, Number, RequireExactKeys, RequireMapping, Text
from trader.training.application.terminal_holdout_execution import ResearchCancelledError, TerminalHoldoutAlreadyOpenedError, TerminalPreparedInputs
from trader.training.domain.evaluation.terminal_inputs import (
    EstimateTerminalWorkingSet, TerminalHoldoutEvidence, TerminalHoldoutPartition, TerminalInputFootprint,
)
from trader.training.domain.evaluation.terminal_holdout import TerminalHoldoutRow, TerminalStrategy
from trader.recommendation.domain.scoring.profile_identity import ScoringProfileId
from trader.training.infra.research.registration_codec import DecodeStateRegistration
from typing import cast


class TerminalHoldoutInputFiles:
    def __init__(self, root: Path, opening_path: Path, *, cancel_requested: Callable[[], bool] | None = None) -> None:
        self._root = root
        self._opening_path = opening_path
        self._cancel = cancel_requested

    def _check(self) -> None:
        if self._cancel is not None and self._cancel():
            raise ResearchCancelledError("research_cancelled")

    def inspect(self) -> TerminalHoldoutEvidence:
        path = self._root / "plan.json"
        self._check()
        encoded = ReadRegularFile(path, maximum_bytes=16 * 1024 * 1024)
        raw = _object(json.loads(encoded))
        RequireExactKeys(raw, {"research_plan_hash", "tomorrow", "d25"}, "terminal_inputs")
        return TerminalHoldoutEvidence(
            Text(raw, "research_plan_hash"),
            _partition(RequireMapping(raw, "tomorrow"), "tomorrow"),
            _partition(RequireMapping(raw, "d25"), "d25"),
        )

    @contextmanager
    def prepare(
        self, evidence: TerminalHoldoutEvidence, strategies: tuple[TerminalStrategy, ...]
    ) -> Iterator[TerminalPreparedInputs]:
        if not strategies or len(set(strategies)) != len(strategies) or any(value not in {"tomorrow", "d25"} for value in strategies):
            raise ValueError("terminal opening requires an explicit strategy set")
        if self.inspect() != evidence:
            raise ValueError("terminal metadata changed after admission")
        footprints = []
        with ExitStack() as resources:
            streams: dict[TerminalStrategy, RegularFileStream] = {}
            for partition in (evidence.tomorrow, evidence.d25):
                if partition.strategy not in strategies:
                    continue
                stream = resources.enter_context(OpenRegularFile(
                    self._root / f"{partition.strategy}.jsonl", maximum_bytes=4 * 1024**3, check=self._check,
                ))
                streams[partition.strategy] = stream
                footprints.append(TerminalInputFootprint(partition.row_count, len(partition.trade_dates), os.fstat(stream.fileno()).st_size))
            yield _PreparedInputs(self, evidence, strategies, streams, EstimateTerminalWorkingSet(tuple(footprints)))

    def _open_once(
        self, evidence: TerminalHoldoutEvidence, strategies: tuple[TerminalStrategy, ...],
        streams: Mapping[TerminalStrategy, RegularFileStream],
    ) -> tuple[tuple[TerminalHoldoutRow, ...], tuple[TerminalHoldoutRow, ...]]:
        self._check()
        if self.inspect() != evidence:
            raise ValueError("terminal metadata changed after admission")
        for stream in streams.values():
            stream.verify()
        marker = self._opening_path
        if marker.is_symlink():
            raise TerminalHoldoutAlreadyOpenedError("terminal_holdout_already_opened")
        opening = json.dumps({"evidence_hash": evidence.content_hash, "strategies": strategies}, sort_keys=True)
        if not PublishImmutable(marker, opening + "\n"):
            raise TerminalHoldoutAlreadyOpenedError("terminal_holdout_already_opened")
        # The exclusive link and its fsynced content precede every outcome read.
        # Sync the containing directory too where Python exposes directory handles.
        if os.name == "posix":
            descriptor = os.open(marker.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        tomorrow = self._read(evidence.tomorrow, streams["tomorrow"]) if "tomorrow" in strategies else ()
        d25 = self._read(evidence.d25, streams["d25"]) if "d25" in strategies else ()
        return tomorrow, d25

    def _read(self, partition: TerminalHoldoutPartition, stream: RegularFileStream) -> tuple[TerminalHoldoutRow, ...]:
        rows: list[TerminalHoldoutRow] = []
        digest = hashlib.sha256()
        state = partition.state_registration
        if state is None:
            raise ValueError("terminal_state_registration_missing")
        states = {day.trade_date: state.states(day) for day in state.days}
        while encoded := stream.readline(16_385):
            self._check()
            if len(encoded) > 16_384 or len(rows) >= partition.row_count:
                raise ValueError("terminal_outcome_line_or_row_limit")
            digest.update(encoded)
            row = _row(_object(json.loads(encoded)))
            if partition.profile_id in {"v2", "v3"} and row.ranking_score != row.local_score:
                raise ValueError("terminal_local_score_profile_ranking_mismatch")
            if states.get(row.trade_date) != (row.market_state, row.volatility_state, row.liquidity_state):
                raise ValueError("terminal_daily_state_differs_from_registration")
            rows.append(row)
        if (
            digest.hexdigest() != partition.rows_sha256 or len(rows) != partition.row_count
            or tuple(sorted({row.trade_date for row in rows})) != partition.trade_dates
        ):
            raise ValueError("terminal outcomes differ from their sealed metadata")
        return tuple(rows)


class _PreparedInputs:
    def __init__(
        self, owner: TerminalHoldoutInputFiles, evidence: TerminalHoldoutEvidence,
        strategies: tuple[TerminalStrategy, ...], streams: Mapping[TerminalStrategy, RegularFileStream], memory_bytes: int,
    ) -> None:
        self._owner, self._evidence, self._strategies = owner, evidence, strategies
        self._streams, self._memory_bytes = streams, memory_bytes

    @property
    def memory_bytes(self) -> int:
        return self._memory_bytes

    def open_once(self) -> tuple[tuple[TerminalHoldoutRow, ...], tuple[TerminalHoldoutRow, ...]]:
        return self._owner._open_once(self._evidence, self._strategies, self._streams)


def _object(raw: object) -> Mapping[str, object]:
    if not isinstance(raw, dict) or any(not isinstance(key, str) for key in raw):
        raise ValueError("terminal input must contain JSON objects")
    return raw


def _partition(raw: Mapping[str, object], strategy: TerminalStrategy) -> TerminalHoldoutPartition:
    RequireExactKeys(raw, {"parent_hash", "candidate_hash", "trade_dates", "rows_sha256", "row_count", "profile_id", "registration_hash", "state_registration"}, "terminal_partition")
    dates = raw["trade_dates"]
    if not isinstance(dates, list) or len(dates) > 20_000 or any(not isinstance(value, str) for value in dates):
        raise ValueError("terminal partition date metadata is invalid")
    digest = Text(raw, "rows_sha256")
    if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise ValueError("terminal row digest is invalid")
    return TerminalHoldoutPartition(
        strategy, Text(raw, "parent_hash"), Text(raw, "candidate_hash"),
        tuple(date.fromisoformat(value) for value in dates), digest, Integer(raw, "row_count", minimum=1),
        cast(ScoringProfileId, Text(raw, "profile_id")), Text(raw, "registration_hash"),
        None if raw["state_registration"] is None else DecodeStateRegistration(RequireMapping(raw, "state_registration")),
    )


def _numbers(raw: Mapping[str, object], name: str) -> tuple[float, ...]:
    values = raw[name]
    if not isinstance(values, list) or len(values) > 4:
        raise ValueError("terminal outcome return vector is invalid")
    return tuple(Number({"value": value}, "value") for value in values)


def _row(raw: Mapping[str, object]) -> TerminalHoldoutRow:
    RequireExactKeys(raw, {
        "trade_date", "code", "board", "industry", "market_state", "volatility_state", "liquidity_state",
        "predicted_net_excess_return", "ranking_score", "local_score", "actual_net_excess_returns", "baseline_net_excess_returns", "selected",
        "baseline_selected", "severe_loss", "baseline_severe_loss", "mae_atr20", "baseline_mae_atr20",
        "exit_untradable", "baseline_exit_untradable", "capacity", "baseline_capacity", "point_in_time_parity",
        "horizon_net_excess_returns", "baseline_horizon_net_excess_returns",
    }, "terminal_row")
    actual, baseline = _numbers(raw, "actual_net_excess_returns"), _numbers(raw, "baseline_net_excess_returns")
    if len(actual) != 3 or len(baseline) != 3:
        raise ValueError("terminal outcomes require the three configured cost scenarios")
    return TerminalHoldoutRow(
        trade_date=date.fromisoformat(Text(raw, "trade_date")), code=Text(raw, "code"), board=Text(raw, "board"),
        industry=Text(raw, "industry"), market_state=Text(raw, "market_state"),
        volatility_state=Text(raw, "volatility_state"), liquidity_state=Text(raw, "liquidity_state"),
        predicted_net_excess_return=Number(raw, "predicted_net_excess_return"),
        ranking_score=Number(raw, "ranking_score"), local_score=Number(raw, "local_score"),
        actual_net_excess_returns=actual,
        baseline_net_excess_returns=baseline,
        selected=Boolean(raw, "selected"), baseline_selected=Boolean(raw, "baseline_selected"),
        severe_loss=Boolean(raw, "severe_loss"), baseline_severe_loss=Boolean(raw, "baseline_severe_loss"),
        mae_atr20=Number(raw, "mae_atr20"), baseline_mae_atr20=Number(raw, "baseline_mae_atr20"),
        exit_untradable=Boolean(raw, "exit_untradable"), baseline_exit_untradable=Boolean(raw, "baseline_exit_untradable"),
        capacity=Number(raw, "capacity"), baseline_capacity=Number(raw, "baseline_capacity"),
        point_in_time_parity=Boolean(raw, "point_in_time_parity"),
        horizon_net_excess_returns=_numbers(raw, "horizon_net_excess_returns"),
        baseline_horizon_net_excess_returns=_numbers(raw, "baseline_horizon_net_excess_returns"),
    )
