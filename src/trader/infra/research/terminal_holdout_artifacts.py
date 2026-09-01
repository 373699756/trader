"""Immutable JSON storage for point-in-time terminal holdout reports."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import date
from pathlib import Path

from trader.application.research.replay_models import canonical_json
from trader.domain.research.terminal_holdout import (
    TerminalHoldoutMetrics,
    TerminalHoldoutReport,
    TerminalStrategy,
)


class TerminalHoldoutArtifactConflictError(RuntimeError):
    """Raised when a sealed report is missing, tampered with, or conflicts."""


class TerminalHoldoutArtifactStore:
    def __init__(self, root: Path, *, strategy: TerminalStrategy | None = None) -> None:
        self._root = root
        self._strategy = strategy

    def write(self, report: TerminalHoldoutReport) -> TerminalHoldoutReport:
        self._validate_strategy(report)
        self._root.mkdir(parents=True, exist_ok=True)
        path = self._root / "report.json"
        if path.exists():
            existing = self.verify()
            if existing.content_hash != report.content_hash:
                raise TerminalHoldoutArtifactConflictError("terminal holdout report identity conflict")
            return existing
        payload = encode_terminal_holdout_report(report)
        payload["content_hash"] = report.content_hash
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=self._root)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(canonical_json(payload))
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                existing = self.verify()
                if existing.content_hash != report.content_hash:
                    raise TerminalHoldoutArtifactConflictError("terminal holdout report identity conflict") from None
                return existing
        finally:
            temporary.unlink(missing_ok=True)
        return self.verify()

    def verify(self) -> TerminalHoldoutReport:
        path = self._root / "report.json"
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise TypeError("terminal holdout report is not an object")
            stored_hash = raw.pop("content_hash")
            if not isinstance(stored_hash, str) or _canonical_hash(raw) != stored_hash:
                raise ValueError("terminal holdout report hash mismatch")
            report = decode_terminal_holdout_report(raw)
            if report.content_hash != stored_hash:
                raise ValueError("terminal holdout report reconstructed hash mismatch")
            self._validate_strategy(report)
            return report
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise TerminalHoldoutArtifactConflictError("terminal holdout report schema or hash is invalid") from exc

    def _validate_strategy(self, report: TerminalHoldoutReport) -> None:
        if self._strategy is not None and report.strategy != self._strategy:
            raise TerminalHoldoutArtifactConflictError("terminal holdout report strategy mismatch")


def decode_terminal_holdout_report(raw: dict[str, object]) -> TerminalHoldoutReport:
    expected = {
        "strategy", "research_identity", "parent_hash", "candidate_hash", "anchor",
        "terminal_holdout_opened", "status", "metrics", "failure_reasons", "terminal_trade_dates",
        "production_authority", "schema_version",
    }
    if set(raw) != expected:
        raise ValueError("terminal holdout report fields are invalid")
    metrics_raw = raw["metrics"]
    if not isinstance(metrics_raw, dict):
        raise TypeError("terminal holdout metrics are invalid")
    metrics = _decode_metrics(metrics_raw)
    dates_raw = raw["terminal_trade_dates"]
    reasons_raw = raw["failure_reasons"]
    if not isinstance(dates_raw, list) or not all(isinstance(item, str) for item in dates_raw):
        raise TypeError("terminal holdout dates are invalid")
    if not isinstance(reasons_raw, list) or not all(isinstance(item, str) for item in reasons_raw):
        raise TypeError("terminal holdout failure reasons are invalid")
    return TerminalHoldoutReport(
        strategy=raw["strategy"],  # type: ignore[arg-type]
        research_identity=_string(raw["research_identity"]),
        parent_hash=_string(raw["parent_hash"]),
        candidate_hash=_string(raw["candidate_hash"]),
        anchor=_string(raw["anchor"]),
        terminal_holdout_opened=_bool(raw["terminal_holdout_opened"]),
        status=raw["status"],  # type: ignore[arg-type]
        metrics=metrics,
        failure_reasons=tuple(reasons_raw),
        terminal_trade_dates=tuple(date.fromisoformat(item) for item in dates_raw),
        production_authority=_bool(raw["production_authority"]),
        schema_version=_string(raw["schema_version"]),
    )


def encode_terminal_holdout_report(report: TerminalHoldoutReport) -> dict[str, object]:
    metrics = report.metrics
    return {
        "strategy": report.strategy,
        "research_identity": report.research_identity,
        "parent_hash": report.parent_hash,
        "candidate_hash": report.candidate_hash,
        "anchor": report.anchor,
        "terminal_holdout_opened": report.terminal_holdout_opened,
        "status": report.status,
        "metrics": _encode_metrics(metrics),
        "failure_reasons": list(report.failure_reasons),
        "terminal_trade_dates": [item.isoformat() for item in report.terminal_trade_dates],
        "production_authority": report.production_authority,
        "schema_version": report.schema_version,
    }


def _encode_metrics(metrics: TerminalHoldoutMetrics) -> dict[str, object]:
    return {
        "evaluated_trade_dates": metrics.evaluated_trade_dates,
        "evaluated_rows": metrics.evaluated_rows,
        "selected_rows": metrics.selected_rows,
        "baseline_selected_rows": metrics.baseline_selected_rows,
        "mean_net_excess_returns": list(metrics.mean_net_excess_returns),
        "baseline_mean_net_excess_returns": list(metrics.baseline_mean_net_excess_returns),
        "paired_net_increments": list(metrics.paired_net_increments),
        "bootstrap_lower_bounds": list(metrics.bootstrap_lower_bounds),
        "severe_loss_rate": metrics.severe_loss_rate,
        "baseline_severe_loss_rate": metrics.baseline_severe_loss_rate,
        "turnover": metrics.turnover,
        "baseline_turnover": metrics.baseline_turnover,
        "rank_ic": metrics.rank_ic,
        "top_bottom_quintile_spread": metrics.top_bottom_quintile_spread,
        "maximum_stock_positive_fraction": metrics.maximum_stock_positive_fraction,
        "top_five_positive_fraction": metrics.top_five_positive_fraction,
        "maximum_board_fraction": metrics.maximum_board_fraction,
        "maximum_industry_count": metrics.maximum_industry_count,
        "capacity": metrics.capacity,
        "baseline_capacity": metrics.baseline_capacity,
        "horizon_mean_net_excess_returns": list(metrics.horizon_mean_net_excess_returns),
        "baseline_horizon_mean_net_excess_returns": list(metrics.baseline_horizon_mean_net_excess_returns),
        "state_sample_counts": [[label, count] for label, count in metrics.state_sample_counts],
    }


def _decode_metrics(raw: dict[str, object]) -> TerminalHoldoutMetrics:
    expected = {
        "evaluated_trade_dates", "evaluated_rows", "selected_rows", "baseline_selected_rows",
        "mean_net_excess_returns", "baseline_mean_net_excess_returns", "paired_net_increments",
        "bootstrap_lower_bounds", "severe_loss_rate", "baseline_severe_loss_rate", "turnover",
        "baseline_turnover", "rank_ic", "top_bottom_quintile_spread", "maximum_stock_positive_fraction",
        "top_five_positive_fraction", "maximum_board_fraction", "maximum_industry_count", "capacity",
        "baseline_capacity", "horizon_mean_net_excess_returns", "baseline_horizon_mean_net_excess_returns",
        "state_sample_counts",
    }
    if set(raw) != expected:
        raise ValueError("terminal holdout metric fields are invalid")
    state = raw["state_sample_counts"]
    if not isinstance(state, list):
        raise TypeError("terminal holdout state counts are invalid")
    state_pairs = tuple((_string(item[0]), _int(item[1])) for item in state if isinstance(item, list) and len(item) == 2)
    if len(state_pairs) != len(state):
        raise TypeError("terminal holdout state count entry is invalid")
    return TerminalHoldoutMetrics(
        _int(raw["evaluated_trade_dates"]), _int(raw["evaluated_rows"]), _int(raw["selected_rows"]), _int(raw["baseline_selected_rows"]),
        _triple(raw["mean_net_excess_returns"]), _triple(raw["baseline_mean_net_excess_returns"]), _triple(raw["paired_net_increments"]),
        _optional_triple(raw["bootstrap_lower_bounds"]), _optional_float(raw["severe_loss_rate"]), _optional_float(raw["baseline_severe_loss_rate"]),
        _float(raw["turnover"]), _float(raw["baseline_turnover"]), _optional_float(raw["rank_ic"]), _optional_float(raw["top_bottom_quintile_spread"]),
        _float(raw["maximum_stock_positive_fraction"]), _float(raw["top_five_positive_fraction"]), _float(raw["maximum_board_fraction"]), _int(raw["maximum_industry_count"]),
        _float(raw["capacity"]), _float(raw["baseline_capacity"]), _floats(raw["horizon_mean_net_excess_returns"]), _floats(raw["baseline_horizon_mean_net_excess_returns"]), state_pairs,
    )


def _canonical_hash(value: object) -> str:
    from hashlib import sha256

    return sha256(canonical_json(value).encode()).hexdigest()


def _string(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("expected string")
    return value


def _bool(value: object) -> bool:
    if not isinstance(value, bool):
        raise TypeError("expected boolean")
    return value


def _int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("expected integer")
    return value


def _float(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("expected number")
    return float(value)


def _optional_float(value: object) -> float | None:
    return None if value is None else _float(value)


def _triple(value: object) -> tuple[float, float, float]:
    if not isinstance(value, list) or len(value) != 3:
        raise TypeError("expected three numbers")
    return (_float(value[0]), _float(value[1]), _float(value[2]))


def _optional_triple(value: object) -> tuple[float | None, float | None, float | None]:
    if not isinstance(value, list) or len(value) != 3:
        raise TypeError("expected three optional numbers")
    return (_optional_float(value[0]), _optional_float(value[1]), _optional_float(value[2]))


def _floats(value: object) -> tuple[float, ...]:
    if not isinstance(value, list):
        raise TypeError("expected numeric list")
    return tuple(_float(item) for item in value)


__all__ = [
    "TerminalHoldoutArtifactConflictError",
    "TerminalHoldoutArtifactStore",
    "decode_terminal_holdout_report",
    "encode_terminal_holdout_report",
]
