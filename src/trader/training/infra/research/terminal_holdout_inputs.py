"""Bounded external terminal inputs with a durable, first-writer opening record."""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping
from datetime import date
from pathlib import Path

from trader.infra.serialization.sealing import PublishImmutable
from trader.infra.regular_file import OpenRegularFileLines, ReadRegularFile
from trader.infra.settings.parser import Boolean, Integer, Number, RequireExactKeys, RequireMapping, Text
from trader.training.application.terminal_holdout_execution import TerminalHoldoutAlreadyOpenedError
from trader.training.domain.evaluation.terminal_inputs import TerminalHoldoutEvidence, TerminalHoldoutPartition
from trader.training.domain.evaluation.terminal_holdout import TerminalHoldoutRow, TerminalStrategy


class TerminalHoldoutInputFiles:
    def __init__(self, root: Path, opening_path: Path) -> None:
        self._root = root
        self._opening_path = opening_path

    def inspect(self) -> TerminalHoldoutEvidence:
        path = self._root / "plan.json"
        encoded = ReadRegularFile(path, maximum_bytes=1024 * 1024)
        raw = _object(json.loads(encoded))
        RequireExactKeys(raw, {"research_plan_hash", "tomorrow", "d25"}, "terminal_inputs")
        return TerminalHoldoutEvidence(
            Text(raw, "research_plan_hash"),
            _partition(RequireMapping(raw, "tomorrow"), "tomorrow"),
            _partition(RequireMapping(raw, "d25"), "d25"),
        )

    def open_once(
        self, evidence: TerminalHoldoutEvidence, strategies: tuple[TerminalStrategy, ...]
    ) -> tuple[tuple[TerminalHoldoutRow, ...], tuple[TerminalHoldoutRow, ...]]:
        if not strategies or len(set(strategies)) != len(strategies) or any(value not in {"tomorrow", "d25"} for value in strategies):
            raise ValueError("terminal opening requires an explicit strategy set")
        if self.inspect() != evidence:
            raise ValueError("terminal metadata changed after admission")
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
        tomorrow = self._read(evidence.tomorrow) if "tomorrow" in strategies else ()
        d25 = self._read(evidence.d25) if "d25" in strategies else ()
        return tomorrow, d25

    def _read(self, partition: TerminalHoldoutPartition) -> tuple[TerminalHoldoutRow, ...]:
        path = self._root / f"{partition.strategy}.jsonl"
        rows: list[TerminalHoldoutRow] = []
        digest = hashlib.sha256()
        with OpenRegularFileLines(
            path, maximum_bytes=4 * 1024**3, maximum_line_bytes=16_384, maximum_lines=partition.row_count,
        ) as lines:
            for encoded in lines:
                digest.update(encoded)
                rows.append(_row(_object(json.loads(encoded))))
        if (
            digest.hexdigest() != partition.rows_sha256 or len(rows) != partition.row_count
            or tuple(sorted({row.trade_date for row in rows})) != partition.trade_dates
        ):
            raise ValueError("terminal outcomes differ from their sealed metadata")
        return tuple(rows)


def _object(raw: object) -> Mapping[str, object]:
    if not isinstance(raw, dict) or any(not isinstance(key, str) for key in raw):
        raise ValueError("terminal input must contain JSON objects")
    return raw


def _partition(raw: Mapping[str, object], strategy: TerminalStrategy) -> TerminalHoldoutPartition:
    RequireExactKeys(raw, {"parent_hash", "candidate_hash", "trade_dates", "rows_sha256", "row_count"}, "terminal_partition")
    dates = raw["trade_dates"]
    if not isinstance(dates, list) or len(dates) > 20_000 or any(not isinstance(value, str) for value in dates):
        raise ValueError("terminal partition date metadata is invalid")
    digest = Text(raw, "rows_sha256")
    if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise ValueError("terminal row digest is invalid")
    return TerminalHoldoutPartition(
        strategy, Text(raw, "parent_hash"), Text(raw, "candidate_hash"),
        tuple(date.fromisoformat(value) for value in dates), digest, Integer(raw, "row_count", minimum=1),
    )


def _numbers(raw: Mapping[str, object], name: str) -> tuple[float, ...]:
    values = raw[name]
    if not isinstance(values, list) or len(values) > 4:
        raise ValueError("terminal outcome return vector is invalid")
    return tuple(Number({"value": value}, "value") for value in values)


def _row(raw: Mapping[str, object]) -> TerminalHoldoutRow:
    RequireExactKeys(raw, {
        "trade_date", "code", "board", "industry", "market_state", "volatility_state", "liquidity_state",
        "predicted_net_excess_return", "actual_net_excess_returns", "baseline_net_excess_returns", "selected",
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
