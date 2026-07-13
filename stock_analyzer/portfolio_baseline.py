from __future__ import annotations

import hashlib
import json
import math
import random
import zlib
from datetime import datetime
from typing import Dict, Iterable, List, Optional

from . import config
from .execution_policy import build_execution_policy, cost_scenarios, market_impact_cost_pct
from .normalization import coerce_number, normalize_code, rename_known_columns
from .production_baseline import production_baseline_id
from .sqlite_support import sqlite_transaction
from .strategy_validation import _compute_outcome, _primary_return_config, validation_baseline_config
from .validation_statistics import block_bootstrap_mean_confidence_interval


COMPARISON_LABELS = {
    "frozen_rule_top_k": "冻结规则等权 Top-K",
    "model_top_k": "挑战模型等权 Top-K",
    "current_rule_top_k": "当前规则等权 Top-K",
    "random_equal_weight": "合格候选等权随机",
    "major_index": "主要指数",
    "cash": "现金",
}


class DailyPortfolioBaselineService:
    def __init__(self, validation_store) -> None:
        self.validation_store = validation_store
        self.db_path = validation_store.db_path
        self._index_history_cache: Dict[str, object] = {}

    def run(
        self,
        provider,
        strategy_name: str = "tomorrow_picks",
        signal_date: str = "",
        days: int = 120,
        ranking_field: str = "score",
        model_id: str = "",
        top_k: int = 0,
        random_seed: Optional[int] = None,
        random_repeats: int = 0,
        index_code: str = "",
        index_label: str = "",
        reuse_settled: bool = False,
    ) -> Dict[str, object]:
        strategy_name = str(strategy_name or "tomorrow_picks")
        resolved_top_k = max(1, int(top_k or getattr(config, "PORTFOLIO_BASELINE_TOP_K", 5)))
        resolved_seed = int(
            getattr(config, "PORTFOLIO_BASELINE_RANDOM_SEED", 20260712)
            if random_seed is None
            else random_seed
        )
        resolved_repeats = max(
            1000,
            int(random_repeats or getattr(config, "PORTFOLIO_BASELINE_RANDOM_REPEATS", 1000)),
        )
        resolved_index_code = normalize_code(
            index_code or getattr(config, "PORTFOLIO_BASELINE_INDEX_CODE", "000300")
        )
        resolved_index_label = str(
            index_label or getattr(config, "PORTFOLIO_BASELINE_INDEX_LABEL", "沪深300")
        )
        dates = [signal_date] if signal_date else self._signal_dates(strategy_name, days)
        results = []
        for date_value in dates:
            results.append(
                self._run_date(
                    provider,
                    strategy_name,
                    date_value,
                    ranking_field=ranking_field,
                    model_id=model_id,
                    top_k=resolved_top_k,
                    random_seed=resolved_seed,
                    random_repeats=resolved_repeats,
                    index_code=resolved_index_code,
                    index_label=resolved_index_label,
                    reuse_settled=reuse_settled,
                )
            )
        report_baseline_id = next(
            (
                str(item.get("portfolio_baseline_id") or "")
                for item in reversed(results)
                if item.get("portfolio_baseline_id")
            ),
            "",
        )
        report = self.report(
            strategy_name,
            days=days,
            ranking_field=ranking_field,
            model_id=model_id,
            top_k=resolved_top_k,
            random_seed=resolved_seed,
            random_repeats=resolved_repeats,
            index_code=resolved_index_code,
            portfolio_baseline_id=report_baseline_id,
        )
        return {
            "ok": all(item.get("status") != "error" for item in results),
            "strategy": strategy_name,
            "processed": len(results),
            "settled": sum(1 for item in results if item.get("status") == "settled"),
            "pending": sum(1 for item in results if item.get("status") == "pending"),
            "unknown": sum(1 for item in results if item.get("status") == "unknown"),
            "results": results,
            "report": report,
        }

    def report(
        self,
        strategy_name: str,
        days: int = 120,
        ranking_field: str = "score",
        model_id: str = "",
        top_k: int = 0,
        random_seed: Optional[int] = None,
        random_repeats: int = 0,
        index_code: str = "",
        include_audit: bool = False,
        portfolio_baseline_id: str = "",
    ) -> Dict[str, object]:
        portfolio_baseline_id = portfolio_baseline_id or build_portfolio_baseline_id(
            strategy_name,
            ranking_field=ranking_field,
            model_id=model_id,
            top_k=top_k or getattr(config, "PORTFOLIO_BASELINE_TOP_K", 5),
            random_seed=(
                getattr(config, "PORTFOLIO_BASELINE_RANDOM_SEED", 20260712)
                if random_seed is None
                else random_seed
            ),
            random_repeats=random_repeats or getattr(config, "PORTFOLIO_BASELINE_RANDOM_REPEATS", 1000),
            index_code=index_code or getattr(config, "PORTFOLIO_BASELINE_INDEX_CODE", "000300"),
        )
        records = self._records(
            strategy_name,
            portfolio_baseline_id,
            days,
            include_audit=include_audit,
        )
        settled = [
            item
            for item in records
            if item.get("status") == "settled"
            and all(
                ((item.get("groups") or {}).get(key) or {}).get("status") == "settled"
                for key in COMPARISON_LABELS
                if key not in {"major_index", "model_top_k"}
            )
        ]
        daily = _build_daily_series(settled)
        groups = {
            key: _group_metrics(daily, key)
            for key in COMPARISON_LABELS
        }
        rule_random_percentile = _paired_random_percentile(settled)
        paired_dates = [item.get("signal_date") for item in settled]
        latest_record = records[-1] if records else {}
        latest = latest_record if include_audit else _compact_report_record(latest_record)
        return {
            "portfolio_baseline_id": portfolio_baseline_id,
            "strategy": strategy_name,
            "model_id": model_id or "frozen_rule",
            "ranking_field": ranking_field,
            "day_count": len(settled),
            "record_count": len(records),
            "pending_day_count": sum(1 for item in records if item.get("status") == "pending"),
            "unknown_day_count": sum(1 for item in records if item.get("status") == "unknown"),
            "paired_dates": paired_dates,
            "random_seed": latest_record.get("random_seed"),
            "random_repeats": latest_record.get("random_repeats"),
            "rule_vs_random_percentile": rule_random_percentile,
            "groups": groups,
            "daily": daily,
            "latest": latest,
        }

    def record(
        self,
        strategy_name: str,
        signal_date: str,
        ranking_field: str = "score",
        model_id: str = "",
        include_audit: bool = False,
    ) -> Dict[str, object]:
        with sqlite_transaction(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT result_json, audit_blob
                FROM daily_portfolio_baselines
                WHERE strategy_name = ? AND signal_date = ? AND model_id = ?
                ORDER BY updated_at DESC
                """,
                (strategy_name, signal_date, model_id or "frozen_rule"),
            ).fetchall()
        for row in rows:
            result = _load_persisted_record(row[0], row[1], include_audit=include_audit)
            if isinstance(result, dict) and str(result.get("ranking_field") or "score") == ranking_field:
                return result if include_audit else _compact_report_record(result)
        return {
            "strategy": strategy_name,
            "signal_date": signal_date,
            "model_id": model_id or "frozen_rule",
            "ranking_field": ranking_field,
            "status": "missing",
        }

    def _run_date(
        self,
        provider,
        strategy_name: str,
        signal_date: str,
        *,
        ranking_field: str,
        model_id: str,
        top_k: int,
        random_seed: int,
        random_repeats: int,
        index_code: str,
        index_label: str,
        reuse_settled: bool,
    ) -> Dict[str, object]:
        batch = self._batch_metadata(strategy_name, signal_date)
        candidates = self.validation_store.candidate_snapshots_for_date(
            signal_date,
            strategy_name,
            str(batch.get("strategy_version") or ""),
        )
        if not candidates:
            return {
                "signal_date": signal_date,
                "status": "unknown",
                "reason": "candidate_pool_unavailable",
            }
        policy = batch.get("execution_policy") or build_execution_policy(strategy_name)
        validation_baseline = validation_baseline_config(strategy_name, execution_policy=policy)
        eligible = [
            _candidate_payload(item)
            for item in candidates
            if item.get("eligible") and item.get("point_in_time_valid", True)
        ]
        if not eligible:
            result = self._empty_day_result(
                strategy_name,
                signal_date,
                candidates,
                ranking_field,
                model_id,
                top_k,
                random_seed,
                random_repeats,
                index_code,
                index_label,
                status="settled",
                reason="no_eligible_candidates",
                policy=policy,
                validation_baseline=validation_baseline,
                batch=batch,
            )
            result["groups"]["major_index"] = _index_group(
                provider,
                index_code,
                index_label,
                signal_date,
                strategy_name,
                self._index_history_cache,
            )
            self._save(result)
            return _compact_run_result(result)

        candidate_hash = _candidate_hash(candidates)
        portfolio_baseline_id = build_portfolio_baseline_id(
            strategy_name,
            ranking_field=ranking_field,
            model_id=model_id,
            top_k=top_k,
            random_seed=random_seed,
            random_repeats=random_repeats,
            index_code=index_code,
            execution_policy_version=str(policy.get("policy_version") or ""),
        )
        if reuse_settled:
            existing = self._existing_record(strategy_name, portfolio_baseline_id, signal_date)
            existing_index = (existing.get("groups") or {}).get("major_index") or {}
            if (
                existing.get("status") == "settled"
                and existing.get("candidate_hash") == candidate_hash
                and existing_index.get("status") == "settled"
            ):
                return _compact_run_result(existing)
        execution_by_code = self._execution_outcomes(
            provider,
            strategy_name,
            signal_date,
            eligible,
            policy,
        )
        counts = _status_counts(execution_by_code.values())
        if counts.get("unknown"):
            status = "unknown"
            reason = "candidate_outcome_unknown"
        elif counts.get("pending"):
            status = "pending"
            reason = "candidate_outcome_pending"
        else:
            status = "settled"
            reason = "settled"

        ranking_coverage_count = sum(1 for item in eligible if _has_ranking_value(item, ranking_field))
        ranking_coverage_pct = round(ranking_coverage_count / max(1, len(eligible)) * 100.0, 2)
        frozen_rows = _rank_candidates(eligible, "score")[:top_k]
        model_rows = _rank_candidates(eligible, ranking_field)[:top_k]
        current_rows = sorted(
            (item for item in eligible if item.get("current_rule_selected")),
            key=lambda item: (int(item.get("selected_rank") or 999999), normalize_code(item.get("code"))),
        )[:top_k]
        frozen_group = _portfolio_group("frozen_rule_top_k", frozen_rows, execution_by_code, top_k, policy)
        model_group = _portfolio_group("model_top_k", model_rows, execution_by_code, top_k, policy)
        if ranking_coverage_count < len(eligible):
            model_group.update(
                {
                    "status": "unknown",
                    "net_return_pct": None,
                    "reason": "ranking_field_incomplete",
                }
            )
        current_group = _portfolio_group("current_rule_top_k", current_rows, execution_by_code, top_k, policy)
        random_group = _random_group(
            eligible,
            execution_by_code,
            top_k,
            seed=_date_seed(random_seed, strategy_name, signal_date),
            repeats=random_repeats,
            rule_return=coerce_number(frozen_group.get("net_return_pct")),
            model_return=coerce_number(model_group.get("net_return_pct"), None),
            policy=policy,
        )
        index_group = _index_group(
            provider,
            index_code,
            index_label,
            signal_date,
            strategy_name,
            self._index_history_cache,
        )
        cash_group = {
            "key": "cash",
            "label": COMPARISON_LABELS["cash"],
            "status": "settled",
            "net_return_pct": 0.0,
            "gross_return_pct": 0.0,
            "position_count": 0,
            "filled_count": 0,
            "unfilled_count": 0,
            "unfilled_rate_pct": 0.0,
            "capacity_utilization_pct": 0.0,
            "industry_concentration_pct": 0.0,
            "holdings": [],
        }
        result = {
            "schema_version": 1,
            "strategy": strategy_name,
            "portfolio_baseline_id": portfolio_baseline_id,
            "production_baseline_id": production_baseline_id(),
            "validation_baseline_id": validation_baseline.get("baseline_id"),
            "execution_policy_version": policy.get("policy_version"),
            "signal_date": signal_date,
            "signal_time": batch.get("signal_time") or "",
            "strategy_version": batch.get("strategy_version") or "",
            "model_id": model_id or "frozen_rule",
            "ranking_field": ranking_field,
            "ranking_coverage_count": ranking_coverage_count,
            "ranking_coverage_pct": ranking_coverage_pct,
            "top_k": top_k,
            "random_seed": random_seed,
            "random_repeats": random_repeats,
            "index_code": index_code,
            "index_label": index_label,
            "status": status,
            "reason": reason,
            "candidate_count": len(candidates),
            "eligible_candidate_count": len(eligible),
            "execution_status_counts": counts,
            "candidate_hash": candidate_hash,
            "groups": {
                "frozen_rule_top_k": frozen_group,
                "model_top_k": model_group,
                "current_rule_top_k": current_group,
                "random_equal_weight": random_group,
                "major_index": index_group,
                "cash": cash_group,
            },
            "audit": {
                "candidate_codes": [normalize_code(item.get("code")) for item in eligible],
                "execution": execution_by_code,
                "ranking": [
                    {
                        "code": normalize_code(item.get("code")),
                        "value": _ranking_value(item, ranking_field),
                        "field": ranking_field,
                    }
                    for item in _rank_candidates(eligible, ranking_field)
                ],
                "execution_policy": policy,
                "validation_baseline": validation_baseline,
            },
            "generated_at": datetime.now().isoformat(timespec="seconds"),
        }
        self._save(result)
        return _compact_run_result(result)

    def _execution_outcomes(
        self,
        provider,
        strategy_name: str,
        signal_date: str,
        candidates: List[Dict[str, object]],
        policy: Dict[str, object],
    ) -> Dict[str, Dict[str, object]]:
        signal_rows = self.validation_store.signals_for_date(signal_date, strategy_name)
        existing = {
            normalize_code(row.get("code")): row
            for row in signal_rows
            if normalize_code(row.get("code"))
        }
        outcomes = {}
        for candidate in candidates:
            code = normalize_code(candidate.get("code"))
            stored = existing.get(code)
            if stored:
                stored_outcome = _stored_execution_outcome(stored, strategy_name)
                if stored_outcome.get("status") in {"settled", "unfilled"}:
                    outcomes[code] = stored_outcome
                    continue
            signal = _synthetic_signal(candidate, strategy_name, signal_date, policy)
            outcome = _compute_outcome(provider, signal)
            outcomes[code] = _computed_execution_outcome(candidate, outcome, policy, strategy_name)
        return outcomes

    def _empty_day_result(
        self,
        strategy_name,
        signal_date,
        candidates,
        ranking_field,
        model_id,
        top_k,
        random_seed,
        random_repeats,
        index_code,
        index_label,
        status,
        reason,
        policy,
        validation_baseline,
        batch,
    ):
        portfolio_baseline_id = build_portfolio_baseline_id(
            strategy_name,
            ranking_field=ranking_field,
            model_id=model_id,
            top_k=top_k,
            random_seed=random_seed,
            random_repeats=random_repeats,
            index_code=index_code,
            execution_policy_version=str(policy.get("policy_version") or ""),
        )
        empty = {
            "status": "settled",
            "net_return_pct": 0.0,
            "gross_return_pct": 0.0,
            "position_count": 0,
            "target_slots": top_k,
            "filled_count": 0,
            "unfilled_count": 0,
            "unfilled_rate_pct": 0.0,
            "capacity_utilization_pct": 0.0,
            "industry_concentration_pct": 0.0,
            "holdings": [],
        }
        groups = {key: {"key": key, "label": label, **empty} for key, label in COMPARISON_LABELS.items()}
        groups["random_equal_weight"].update(
            {
                "random_seed": _date_seed(random_seed, strategy_name, signal_date),
                "random_repeats": random_repeats,
                "p05_return_pct": 0.0,
                "median_return_pct": 0.0,
                "p95_return_pct": 0.0,
                "rule_percentile": 100.0,
                "model_percentile": 100.0,
                "path_returns_pct": [0.0] * random_repeats,
                "sample_codes": [[] for _ in range(random_repeats)],
                "sample_filled_codes": [[] for _ in range(random_repeats)],
            }
        )
        return {
            "schema_version": 1,
            "strategy": strategy_name,
            "portfolio_baseline_id": portfolio_baseline_id,
            "production_baseline_id": production_baseline_id(),
            "validation_baseline_id": validation_baseline.get("baseline_id"),
            "execution_policy_version": policy.get("policy_version"),
            "signal_date": signal_date,
            "signal_time": batch.get("signal_time") or "",
            "strategy_version": batch.get("strategy_version") or "",
            "model_id": model_id or "frozen_rule",
            "ranking_field": ranking_field,
            "top_k": top_k,
            "random_seed": random_seed,
            "random_repeats": random_repeats,
            "index_code": index_code,
            "index_label": index_label,
            "status": status,
            "reason": reason,
            "candidate_count": len(candidates),
            "eligible_candidate_count": 0,
            "candidate_hash": _sha256(candidates),
            "groups": groups,
            "audit": {"candidate_codes": [], "execution": {}, "ranking": []},
            "generated_at": datetime.now().isoformat(timespec="seconds"),
        }

    def _signal_dates(self, strategy_name: str, days: int) -> List[str]:
        rows = self.validation_store.list_signal_dates(strategy_name)
        dates = []
        for row in rows:
            date_value = str(row.get("signal_date") or "")
            if date_value and date_value not in dates:
                dates.append(date_value)
            if len(dates) >= max(1, int(days)):
                break
        return sorted(dates)

    def _batch_metadata(self, strategy_name: str, signal_date: str) -> Dict[str, object]:
        with sqlite_transaction(self.db_path) as conn:
            row = conn.execute(
                """
                SELECT signal_time, strategy_version, execution_policy_json
                FROM strategy_signal_batches
                WHERE strategy_name = ? AND signal_date = ? AND candidate_count > 0
                ORDER BY CASE WHEN lower(strategy_version) LIKE '%replay%' THEN 1 ELSE 0 END,
                         signal_time DESC
                LIMIT 1
                """,
                (strategy_name, signal_date),
            ).fetchone()
        if not row:
            return {}
        try:
            policy = json.loads(row[2] or "{}")
        except Exception:
            policy = {}
        return {
            "signal_time": str(row[0] or ""),
            "strategy_version": str(row[1] or ""),
            "execution_policy": policy if isinstance(policy, dict) else {},
        }

    def _save(self, result: Dict[str, object]) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        result_json, audit_blob = _serialize_persisted_record(result)
        with sqlite_transaction(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO daily_portfolio_baselines
                (strategy_name, portfolio_baseline_id, signal_date, signal_time, strategy_version,
                 validation_baseline_id, model_id, status, candidate_hash, result_json, audit_blob,
                 created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(strategy_name, portfolio_baseline_id, signal_date) DO UPDATE SET
                  signal_time=excluded.signal_time,
                  strategy_version=excluded.strategy_version,
                  validation_baseline_id=excluded.validation_baseline_id,
                  model_id=excluded.model_id,
                  status=excluded.status,
                  candidate_hash=excluded.candidate_hash,
                  result_json=excluded.result_json,
                  audit_blob=excluded.audit_blob,
                  updated_at=excluded.updated_at
                """,
                (
                    result.get("strategy"),
                    result.get("portfolio_baseline_id"),
                    result.get("signal_date"),
                    result.get("signal_time", ""),
                    result.get("strategy_version", ""),
                    result.get("validation_baseline_id", ""),
                    result.get("model_id", ""),
                    result.get("status", "pending"),
                    result.get("candidate_hash", ""),
                    result_json,
                    audit_blob,
                    now,
                    now,
                ),
            )

    def _records(
        self,
        strategy_name: str,
        portfolio_baseline_id: str,
        days: int,
        *,
        include_audit: bool = False,
    ) -> List[Dict[str, object]]:
        with sqlite_transaction(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT result_json, audit_blob
                FROM daily_portfolio_baselines
                WHERE strategy_name = ? AND portfolio_baseline_id = ?
                ORDER BY signal_date DESC
                LIMIT ?
                """,
                (strategy_name, portfolio_baseline_id, max(1, int(days))),
            ).fetchall()
        records = []
        for row in reversed(rows):
            item = _load_persisted_record(
                row[0],
                row[1],
                include_audit=include_audit,
                include_random_metrics=True,
            )
            if isinstance(item, dict):
                records.append(item)
        if records:
            latest_policy = str(records[-1].get("execution_policy_version") or "")
            records = [
                item
                for item in records
                if str(item.get("execution_policy_version") or "") == latest_policy
            ]
        return records

    def _existing_record(
        self,
        strategy_name: str,
        portfolio_baseline_id: str,
        signal_date: str,
    ) -> Dict[str, object]:
        with sqlite_transaction(self.db_path) as conn:
            row = conn.execute(
                """
                SELECT result_json, audit_blob
                FROM daily_portfolio_baselines
                WHERE strategy_name = ? AND portfolio_baseline_id = ? AND signal_date = ?
                """,
                (strategy_name, portfolio_baseline_id, signal_date),
            ).fetchone()
        if not row:
            return {}
        result = _load_persisted_record(row[0], row[1])
        return result if isinstance(result, dict) else {}


def build_portfolio_baseline_id(
    strategy_name: str,
    *,
    ranking_field: str = "score",
    model_id: str = "",
    top_k: int = 5,
    random_seed: int = 20260712,
    random_repeats: int = 1000,
    index_code: str = "000300",
    execution_policy_version: str = "",
) -> str:
    payload = {
        "version": getattr(config, "PORTFOLIO_BASELINE_VERSION", "daily_equal_weight_v1"),
        "production_baseline_id": production_baseline_id(),
        "strategy": strategy_name,
        "ranking_field": ranking_field,
        "model_id": model_id or "frozen_rule",
        "top_k": int(top_k),
        "random_seed": int(random_seed),
        "random_repeats": max(1000, int(random_repeats)),
        "index_code": normalize_code(index_code),
        "execution_policy_version": str(
            execution_policy_version or build_execution_policy(strategy_name).get("policy_version") or ""
        ),
    }
    return "portfolio_{}__{}".format(payload["version"], _sha256(payload)[:16])


def _candidate_payload(item: Dict[str, object]) -> Dict[str, object]:
    raw = item.get("raw") if isinstance(item.get("raw"), dict) else {}
    quote = raw.get("quote") if isinstance(raw.get("quote"), dict) else {}
    candidate = raw.get("candidate") if isinstance(raw.get("candidate"), dict) else {}
    scored = raw.get("scored") if isinstance(raw.get("scored"), dict) else {}
    selected = raw.get("selected") if isinstance(raw.get("selected"), dict) else {}
    model_input = (item.get("feature_values") or {}).get("model_input") or {}
    merged = {**quote, **candidate, **model_input, **scored}
    return {
        **merged,
        "code": normalize_code(item.get("code")),
        "name": item.get("name") or merged.get("name") or "",
        "industry": item.get("industry") or merged.get("industry") or "未分类",
        "market": item.get("market") or merged.get("market") or "",
        "market_label": merged.get("market_label") or item.get("market") or "",
        "score": coerce_number(item.get("score"), coerce_number(merged.get("score"))),
        "frozen_score_available": bool(scored),
        "rank": int(item.get("rank") or 0),
        "selected": bool(item.get("selected")),
        "current_rule_selected": bool(
            item.get("selected")
            and selected.get("execution_allowed") is not False
            and str(selected.get("tier") or "primary_watch") == "primary_watch"
        ),
        "selected_rank": int(selected.get("rank") or 0),
        "eligible": bool(item.get("eligible")),
        "point_in_time_valid": bool(item.get("point_in_time_valid", True)),
    }


def _rank_candidates(rows: Iterable[Dict[str, object]], ranking_field: str) -> List[Dict[str, object]]:
    return sorted(
        (dict(item) for item in rows),
        key=lambda item: (
            -_ranking_value(item, ranking_field),
            int(item.get("rank") or 999999),
            normalize_code(item.get("code")),
        ),
    )


def _ranking_value(item: Dict[str, object], ranking_field: str) -> float:
    value = item.get(ranking_field)
    if value is None and ranking_field != "score":
        value = (item.get("feature_values") or {}).get("model_input", {}).get(ranking_field)
    return coerce_number(value, -1e18)


def _has_ranking_value(item: Dict[str, object], ranking_field: str) -> bool:
    if ranking_field == "score" and not item.get("frozen_score_available"):
        return False
    value = item.get(ranking_field)
    if value is None and ranking_field != "score":
        value = (item.get("feature_values") or {}).get("model_input", {}).get(ranking_field)
    number = coerce_number(value, None)
    return number is not None and math.isfinite(number)


def _synthetic_signal(
    candidate: Dict[str, object],
    strategy_name: str,
    signal_date: str,
    policy: Dict[str, object],
) -> Dict[str, object]:
    return {
        **candidate,
        "strategy_name": strategy_name,
        "signal_date": signal_date,
        "price_at_signal": coerce_number(candidate.get("price")),
        "turnover": coerce_number(candidate.get("turnover")),
        "market": candidate.get("market_label") or candidate.get("market") or "",
        "execution_policy_json": policy,
    }


def _computed_execution_outcome(
    candidate: Dict[str, object],
    outcome: Optional[Dict[str, object]],
    policy: Dict[str, object],
    strategy_name: str,
) -> Dict[str, object]:
    if not outcome:
        return {"code": candidate.get("code"), "status": "unknown", "reason": "missing_outcome"}
    status = str(outcome.get("label_status") or ("unfilled" if outcome.get("excluded") else "unknown"))
    if status != "settled":
        return {
            "code": candidate.get("code"),
            "status": status,
            "reason": outcome.get("status_reason") or outcome.get("skip_reason") or status,
            "net_return_pct": None,
            "gross_return_pct": None,
            "trade_cost_pct": 0.0,
            "capacity_utilization_pct": _capacity_utilization(candidate, policy),
            "raw_prices": outcome.get("raw_prices") or [],
        }
    primary_field, _, _ = _primary_return_config(strategy_name)
    gross = coerce_number(outcome.get(primary_field))
    return {
        "code": candidate.get("code"),
        "status": "settled",
        "reason": "settled",
        "entry_date": outcome.get("next_trade_date") or "",
        "exit_date": _primary_exit_date(strategy_name, outcome),
        "gross_return_pct": round(gross, 4),
        "raw_prices": outcome.get("raw_prices") or [],
    }


def _stored_execution_outcome(row: Dict[str, object], strategy_name: str) -> Dict[str, object]:
    status = str(row.get("label_status") or "")
    if not status:
        status = "unfilled" if row.get("skip_reason") else "settled" if row.get("outcome_updated_at") else "pending"
    gross = row.get("gross_return_pct")
    net = row.get("net_return_pct")
    if gross is None and status == "settled":
        gross = row.get("stored_primary_return")
    if net is None and status == "settled":
        net = row.get("stored_primary_return_net")
    return {
        "code": normalize_code(row.get("code")),
        "status": status,
        "reason": row.get("reason") or row.get("skip_reason") or status,
        "entry_date": row.get("next_trade_date") or "",
        "exit_date": _primary_exit_date(strategy_name, row),
        "gross_return_pct": coerce_number(gross, None),
        "stored_trade_cost_pct": (
            round(
                coerce_number(row.get("fee_pct"))
                + coerce_number(row.get("slippage_pct"))
                + coerce_number(row.get("impact_pct")),
                4,
            )
            if status == "settled"
            else 0.0
        ),
        "stored_net_return_pct": coerce_number(net, None),
        "raw_prices": row.get("raw_prices") or [],
    }


def _primary_exit_date(strategy_name: str, outcome: Dict[str, object]) -> str:
    primary_field, _, _ = _primary_return_config(strategy_name)
    if primary_field in {"exit_return", "signal_exit_return"}:
        return str(outcome.get("exit_date") or outcome.get("next_trade_date") or "")
    return str(outcome.get("next_trade_date") or "")


def _portfolio_group(
    key: str,
    rows: List[Dict[str, object]],
    outcomes: Dict[str, Dict[str, object]],
    top_k: int,
    policy: Dict[str, object],
) -> Dict[str, object]:
    target_weight = 1.0 / max(1, top_k)
    gross = 0.0
    net = 0.0
    filled = 0
    unfilled = 0
    pending = 0
    capacity = 0.0
    holdings = []
    industry_weights: Dict[str, float] = {}
    for row in rows:
        code = normalize_code(row.get("code"))
        outcome = outcomes.get(code) or {"status": "unknown", "reason": "missing_execution"}
        status = str(outcome.get("status") or "unknown")
        if status == "settled":
            weighted_row = dict(row)
            weighted_row["suggested_weight"] = target_weight * 100.0
            trade_cost = coerce_number((cost_scenarios(weighted_row, policy).get("base") or {}).get("total_pct"))
            capacity_value = _capacity_utilization(weighted_row, policy)
            net_return = coerce_number(outcome.get("gross_return_pct")) - trade_cost
            filled += 1
            gross += target_weight * coerce_number(outcome.get("gross_return_pct"))
            net += target_weight * net_return
            industry = str(row.get("industry") or "未分类")
            industry_weights[industry] = industry_weights.get(industry, 0.0) + target_weight
        elif status == "unfilled":
            trade_cost = 0.0
            capacity_value = 0.0
            net_return = None
            unfilled += 1
        else:
            trade_cost = None
            capacity_value = 0.0
            net_return = None
            pending += 1
        capacity += target_weight * capacity_value
        holdings.append(
            {
                "code": code,
                "name": row.get("name") or "",
                "industry": row.get("industry") or "未分类",
                "target_weight_pct": round(target_weight * 100.0, 4),
                "execution_status": status,
                "reason": outcome.get("reason") or "",
                "gross_return_pct": outcome.get("gross_return_pct"),
                "net_return_pct": round(net_return, 4) if net_return is not None else None,
                "trade_cost_pct": round(trade_cost, 4) if trade_cost is not None else None,
                "capacity_utilization_pct": round(capacity_value, 4),
            }
        )
    status = "unknown" if pending else "settled"
    return {
        "key": key,
        "label": COMPARISON_LABELS[key],
        "status": status,
        "net_return_pct": round(net, 4) if status == "settled" else None,
        "gross_return_pct": round(gross, 4) if status == "settled" else None,
        "position_count": len(rows),
        "target_slots": top_k,
        "filled_count": filled,
        "unfilled_count": unfilled,
        "pending_count": pending,
        "unfilled_rate_pct": round(unfilled / max(1, len(rows)) * 100.0, 4),
        "capacity_utilization_pct": round(capacity, 4),
        "industry_concentration_pct": round(max(industry_weights.values(), default=0.0) * 100.0, 4),
        "holdings": holdings,
    }


def _random_group(
    candidates,
    outcomes,
    top_k,
    *,
    seed,
    repeats,
    rule_return,
    model_return,
    policy,
):
    rng = random.Random(seed)
    sample_size = min(top_k, len(candidates))
    returns = []
    samples = []
    sample_metrics = []
    sample_filled_codes = []
    for _ in range(repeats):
        selected = rng.sample(candidates, sample_size) if sample_size else []
        group = _portfolio_group("frozen_rule_top_k", selected, outcomes, top_k, policy)
        value = group.get("net_return_pct")
        returns.append(coerce_number(value))
        samples.append([normalize_code(item.get("code")) for item in selected])
        sample_metrics.append(group)
        sample_filled_codes.append(
            [
                normalize_code(item.get("code"))
                for item in group.get("holdings") or []
                if item.get("execution_status") == "settled"
            ]
        )
    ordered = sorted(returns)
    mean_return = sum(returns) / len(returns) if returns else 0.0
    percentile = sum(1 for value in returns if value <= rule_return) / max(1, len(returns)) * 100.0
    model_percentile = (
        sum(1 for value in returns if value <= model_return) / max(1, len(returns)) * 100.0
        if model_return is not None
        else None
    )
    template = _portfolio_group("random_equal_weight", candidates[:sample_size], outcomes, top_k, policy)
    template.update(
        {
            "label": COMPARISON_LABELS["random_equal_weight"],
            "net_return_pct": round(mean_return, 4),
            "gross_return_pct": None,
            "random_seed": seed,
            "random_repeats": repeats,
            "p05_return_pct": _quantile(ordered, 0.05),
            "median_return_pct": _quantile(ordered, 0.5),
            "p95_return_pct": _quantile(ordered, 0.95),
            "rule_percentile": round(percentile, 2),
            "model_percentile": round(model_percentile, 2) if model_percentile is not None else None,
            "unfilled_rate_pct": _average(item.get("unfilled_rate_pct") for item in sample_metrics),
            "capacity_utilization_pct": _average(
                item.get("capacity_utilization_pct") for item in sample_metrics
            ),
            "industry_concentration_pct": _average(
                item.get("industry_concentration_pct") for item in sample_metrics
            ),
            "path_returns_pct": [round(value, 6) for value in returns],
            "sample_codes": samples,
            "sample_filled_codes": sample_filled_codes,
        }
    )
    return template


def _index_group(provider, code, label, signal_date, strategy_name, history_cache=None):
    _, holding_days, _ = _primary_return_config(strategy_name)
    period = _history_period_return(provider, code, signal_date, holding_days, history_cache=history_cache)
    value = period.get("return_pct")
    status = "settled" if value is not None else "unknown"
    return {
        "key": "major_index",
        "label": label,
        "code": code,
        "status": status,
        "reason": "settled" if status == "settled" else "index_history_unavailable",
        "net_return_pct": value,
        "gross_return_pct": value,
        "position_count": 1,
        "filled_count": 1 if status == "settled" else 0,
        "unfilled_count": 0,
        "unfilled_rate_pct": 0.0,
        "capacity_utilization_pct": 0.0,
        "industry_concentration_pct": 0.0,
        "period": period,
        "holdings": [],
    }


def _history_period_return(provider, code, signal_date, holding_days, history_cache=None):
    period = {
        "signal_date": signal_date,
        "entry_date": "",
        "exit_date": "",
        "holding_days": max(1, int(holding_days or 1)),
        "return_pct": None,
    }
    if not signal_date:
        return period
    history_cache = history_cache if isinstance(history_cache, dict) else {}
    if code in history_cache:
        history = history_cache[code]
    else:
        try:
            history_fn = getattr(provider, "get_index_history", None)
            if not callable(history_fn):
                history_fn = provider.get_history
            history = history_fn(code, days=int(getattr(config, "PAPER_TRADING_HISTORY_DAYS", 220)))
        except Exception:
            history = None
        history_cache[code] = history
    if history is None or history.empty or "trade_date" not in history.columns:
        return period
    frame = rename_known_columns(history.copy())
    if "price" not in frame.columns:
        return period
    frame["_date"] = frame["trade_date"].astype(str).str.replace("-", "", regex=False)
    signal_key = str(signal_date).replace("-", "")
    window = frame[frame["_date"] > signal_key].sort_values("_date").head(period["holding_days"])
    if len(window) < period["holding_days"]:
        return period
    entry = coerce_number(window.iloc[0].get("open")) or coerce_number(window.iloc[0].get("price"))
    exit_price = coerce_number(window.iloc[-1].get("price"))
    if entry <= 0 or exit_price <= 0:
        return period
    period.update(
        {
            "entry_date": str(window.iloc[0].get("_date") or ""),
            "exit_date": str(window.iloc[-1].get("_date") or ""),
            "return_pct": round((exit_price / entry - 1.0) * 100.0, 4),
        }
    )
    return period


def _capacity_utilization(row, policy):
    impact = market_impact_cost_pct(row, policy)
    limit = max(0.0001, coerce_number(getattr(config, "MAX_ACCEPTABLE_IMPACT_PCT", 1.0), 1.0))
    return round(impact / limit * 100.0, 4)


def _stored_capacity_utilization(row):
    impact = coerce_number(row.get("impact_pct"))
    limit = max(0.0001, coerce_number(getattr(config, "MAX_ACCEPTABLE_IMPACT_PCT", 1.0), 1.0))
    return round(impact / limit * 100.0, 4)


def _status_counts(outcomes: Iterable[Dict[str, object]]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for item in outcomes:
        status = str(item.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    return counts


def _date_seed(seed: int, strategy_name: str, signal_date: str) -> int:
    raw = "{}|{}|{}".format(seed, strategy_name, signal_date)
    return int(hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16], 16)


def _quantile(values: List[float], probability: float):
    if not values:
        return None
    position = (len(values) - 1) * max(0.0, min(1.0, probability))
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return round(values[lower], 4)
    weight = position - lower
    return round(values[lower] * (1.0 - weight) + values[upper] * weight, 4)


def _build_daily_series(records: List[Dict[str, object]]) -> List[Dict[str, object]]:
    navs = {key: 100.0 for key in COMPARISON_LABELS}
    highs = dict(navs)
    previous_weights: Dict[str, Dict[str, float]] = {}
    previous_random_paths: List[Dict[str, float]] = []
    result = []
    for record in records:
        groups = record.get("groups") or {}
        daily_groups = {}
        for key in COMPARISON_LABELS:
            group = groups.get(key) or {}
            group_status = str(group.get("status") or "unknown")
            if group_status != "settled":
                daily_groups[key] = {
                    "label": COMPARISON_LABELS[key],
                    "status": group_status,
                    "net_return_pct": None,
                    "cumulative_return_pct": None,
                    "nav": None,
                    "drawdown_pct": None,
                    "turnover_pct": 0.0,
                    "industry_concentration_pct": 0.0,
                    "capacity_utilization_pct": 0.0,
                    "unfilled_rate_pct": 0.0,
                }
                continue
            value = coerce_number(group.get("net_return_pct"))
            navs[key] = round(navs[key] * (1.0 + value / 100.0), 8)
            highs[key] = max(highs[key], navs[key])
            if key == "random_equal_weight":
                target_slots = max(1, int(group.get("target_slots") or 1))
                current_random_paths = [
                    _equal_weight_map(codes, target_slots)
                    for codes in group.get("sample_filled_codes") or []
                ]
                if previous_random_paths and current_random_paths:
                    turnover = _average(
                        _weight_turnover_pct(previous, current)
                        for previous, current in zip(previous_random_paths, current_random_paths)
                    )
                else:
                    turnover = _average(
                        _weight_turnover_pct({"__cash__": 100.0}, current)
                        for current in current_random_paths
                    )
                previous_random_paths = current_random_paths
            else:
                weights = _group_weight_map(group)
                turnover = _weight_turnover_pct(
                    previous_weights.get(key, {"__cash__": 100.0}),
                    weights,
                )
                previous_weights[key] = weights
            daily_groups[key] = {
                "label": COMPARISON_LABELS[key],
                "status": "settled",
                "net_return_pct": round(value, 4),
                "cumulative_return_pct": round(navs[key] - 100.0, 4),
                "nav": round(navs[key], 6),
                "drawdown_pct": round((navs[key] / highs[key] - 1.0) * 100.0, 4),
                "turnover_pct": turnover,
                "industry_concentration_pct": coerce_number(group.get("industry_concentration_pct")),
                "capacity_utilization_pct": coerce_number(group.get("capacity_utilization_pct")),
                "unfilled_rate_pct": coerce_number(group.get("unfilled_rate_pct")),
            }
        result.append(
            {
                "signal_date": record.get("signal_date"),
                "groups": daily_groups,
                "rule_random_percentile": (groups.get("random_equal_weight") or {}).get("rule_percentile"),
            }
        )
    return result


def _group_metrics(daily: List[Dict[str, object]], key: str) -> Dict[str, object]:
    rows = [
        (item.get("groups") or {}).get(key) or {}
        for item in daily
        if ((item.get("groups") or {}).get(key) or {}).get("status") == "settled"
    ]
    returns = [coerce_number(item.get("net_return_pct")) for item in rows]
    downside = [value for value in returns if value < 0]
    downside_deviation = math.sqrt(sum(value * value for value in downside) / len(downside)) if downside else 0.0
    average = sum(returns) / len(returns) if returns else 0.0
    return_ci = block_bootstrap_mean_confidence_interval(returns, samples=500)
    sortino = average / downside_deviation * math.sqrt(252.0) if downside_deviation > 0 else None
    return {
        "label": COMPARISON_LABELS[key],
        "status": "settled" if rows else "unknown",
        "day_count": len(rows),
        "total_return_pct": coerce_number(rows[-1].get("cumulative_return_pct")) if rows else None,
        "avg_daily_net_return_pct": round(average, 4) if rows else None,
        "avg_daily_net_return_ci95_low": return_ci[0],
        "avg_daily_net_return_ci95_high": return_ci[1],
        "return_ci_method": "moving_block_bootstrap",
        "max_drawdown_pct": min((coerce_number(item.get("drawdown_pct")) for item in rows), default=None),
        "sortino": round(sortino, 4) if sortino is not None else None,
        "avg_turnover_pct": _average(item.get("turnover_pct") for item in rows),
        "avg_industry_concentration_pct": _average(item.get("industry_concentration_pct") for item in rows),
        "avg_capacity_utilization_pct": _average(item.get("capacity_utilization_pct") for item in rows),
        "avg_unfilled_rate_pct": _average(item.get("unfilled_rate_pct") for item in rows),
    }


def _paired_random_percentile(records: List[Dict[str, object]]):
    if not records:
        return None
    path_returns = []
    rule_nav = 1.0
    for index, record in enumerate(records):
        groups = record.get("groups") or {}
        rule_return = coerce_number((groups.get("frozen_rule_top_k") or {}).get("net_return_pct"))
        rule_nav *= 1.0 + rule_return / 100.0
        paths = (groups.get("random_equal_weight") or {}).get("path_returns_pct") or []
        if index == 0:
            path_returns = [1.0 for _ in paths]
        for path_index, value in enumerate(paths[: len(path_returns)]):
            path_returns[path_index] *= 1.0 + coerce_number(value) / 100.0
    if not path_returns:
        return None
    return round(sum(1 for value in path_returns if value <= rule_nav) / len(path_returns) * 100.0, 2)


def _group_weight_map(group: Dict[str, object]) -> Dict[str, float]:
    weights = {
        normalize_code(item.get("code")): coerce_number(item.get("target_weight_pct"))
        for item in group.get("holdings") or []
        if item.get("execution_status") == "settled" and normalize_code(item.get("code"))
    }
    weights["__cash__"] = max(0.0, 100.0 - sum(weights.values()))
    return weights


def _equal_weight_map(codes: Iterable[str], target_slots: int) -> Dict[str, float]:
    weight = 100.0 / max(1, int(target_slots))
    weights = {normalize_code(code): weight for code in codes if normalize_code(code)}
    weights["__cash__"] = max(0.0, 100.0 - sum(weights.values()))
    return weights


def _weight_turnover_pct(previous: Dict[str, float], current: Dict[str, float]) -> float:
    keys = set(previous) | set(current)
    return round(
        sum(abs(coerce_number(current.get(key)) - coerce_number(previous.get(key))) for key in keys) / 2.0,
        4,
    )


def _average(values: Iterable[object]) -> float:
    items = [coerce_number(value) for value in values]
    return round(sum(items) / len(items), 4) if items else 0.0


def _serialize_persisted_record(result: Dict[str, object]):
    summary = dict(result)
    groups = {
        key: dict(value) if isinstance(value, dict) else value
        for key, value in (result.get("groups") or {}).items()
    }
    summary["groups"] = groups
    audit_payload = {"audit": summary.pop("audit", {})}
    random_group = groups.get("random_equal_weight")
    if isinstance(random_group, dict):
        audit_payload["random_equal_weight"] = {
            "path_returns_pct": random_group.pop("path_returns_pct", []),
        }
        random_group.pop("sample_codes", None)
        random_group.pop("sample_filled_codes", None)
    result_json = json.dumps(summary, ensure_ascii=False, sort_keys=True, default=str)
    audit_json = json.dumps(
        audit_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return result_json, zlib.compress(audit_json, level=9)


def _load_persisted_record(
    result_json,
    audit_blob,
    *,
    include_audit: bool = False,
    include_random_metrics: bool = False,
) -> Dict[str, object]:
    try:
        result = json.loads(result_json or "{}")
    except Exception:
        return {}
    if not isinstance(result, dict) or not audit_blob or not (include_audit or include_random_metrics):
        return result if isinstance(result, dict) else {}
    try:
        audit_payload = json.loads(zlib.decompress(bytes(audit_blob)).decode("utf-8"))
    except Exception:
        return result
    if not isinstance(audit_payload, dict):
        return result
    audit = audit_payload.get("audit")
    if include_audit and isinstance(audit, dict):
        result["audit"] = audit
    random_audit = audit_payload.get("random_equal_weight")
    random_group = (result.get("groups") or {}).get("random_equal_weight")
    if isinstance(random_group, dict) and isinstance(random_audit, dict):
        paths = random_audit.get("path_returns_pct")
        if isinstance(paths, list):
            random_group["path_returns_pct"] = paths
        _restore_random_samples(
            result,
            random_group,
            audit if isinstance(audit, dict) else {},
            include_samples=include_audit,
        )
    return result


def _restore_random_samples(
    result: Dict[str, object],
    random_group: Dict[str, object],
    audit: Dict[str, object],
    *,
    include_samples: bool,
) -> None:
    candidate_codes = [
        normalize_code(code)
        for code in audit.get("candidate_codes") or []
        if normalize_code(code)
    ]
    execution = audit.get("execution") if isinstance(audit.get("execution"), dict) else {}
    repeats = max(0, int(random_group.get("random_repeats") or result.get("random_repeats") or 0))
    target_slots = max(1, int(random_group.get("target_slots") or result.get("top_k") or 1))
    sample_size = min(target_slots, len(candidate_codes))
    rng = random.Random(int(random_group.get("random_seed") or 0))
    samples = [] if include_samples else None
    filled_samples = []
    for _ in range(repeats):
        selected = rng.sample(candidate_codes, sample_size) if sample_size else []
        if samples is not None:
            samples.append(selected)
        filled_samples.append(
            [
                code
                for code in selected
                if str((execution.get(code) or {}).get("status") or "unknown") == "settled"
            ]
        )
    if samples is not None:
        random_group["sample_codes"] = samples
    random_group["sample_filled_codes"] = filled_samples


def _compact_run_result(result: Dict[str, object]) -> Dict[str, object]:
    return {
        "signal_date": result.get("signal_date"),
        "status": result.get("status"),
        "reason": result.get("reason"),
        "portfolio_baseline_id": result.get("portfolio_baseline_id"),
        "candidate_count": result.get("candidate_count"),
        "eligible_candidate_count": result.get("eligible_candidate_count"),
        "execution_status_counts": result.get("execution_status_counts") or {},
        "groups": {
            key: {
                field: value.get(field)
                for field in (
                    "label",
                    "status",
                    "net_return_pct",
                    "position_count",
                    "unfilled_rate_pct",
                    "capacity_utilization_pct",
                    "industry_concentration_pct",
                    "rule_percentile",
                )
            }
            for key, value in (result.get("groups") or {}).items()
        },
    }


def _compact_report_record(result: Dict[str, object]) -> Dict[str, object]:
    if not result:
        return {}
    compact = _compact_run_result(result)
    compact.update(
        {
            "model_id": result.get("model_id"),
            "ranking_field": result.get("ranking_field"),
            "ranking_coverage_pct": result.get("ranking_coverage_pct"),
            "top_k": result.get("top_k"),
            "random_seed": result.get("random_seed"),
            "random_repeats": result.get("random_repeats"),
            "index_code": result.get("index_code"),
            "index_label": result.get("index_label"),
            "execution_policy_version": result.get("execution_policy_version"),
        }
    )
    return compact


def _candidate_hash(candidates: List[Dict[str, object]]) -> str:
    return _sha256(
        [
            {
                "code": item.get("code"),
                "eligible": item.get("eligible"),
                "selected": item.get("selected"),
                "score": item.get("score"),
                "rank": item.get("rank"),
            }
            for item in candidates
        ]
    )


def _sha256(value) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
