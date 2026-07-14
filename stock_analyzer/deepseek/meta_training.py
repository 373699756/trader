from __future__ import annotations

from collections import defaultdict
import json
import math
import random
import sqlite3
from typing import Dict, Iterable, List, Tuple

import numpy as np

from .. import config
from ..execution_policy import cost_scenarios
from ..normalization import coerce_number, normalize_code
from ..portfolio_baseline import DailyPortfolioBaselineService
from ..strategies.types import storage_strategy_name
from .feature_schema import FEATURE_SCHEMA_VERSION, abstain_feature, prompt_version
from .meta_model import META_FEATURES, build_meta_artifact, meta_feature_vector, save_meta_artifact


LOCAL_ABLATION_FEATURES = ("local_score", "deepseek_missing", "abstain")


class DeepSeekMetaTrainingService:
    """Build expanding-window shadow artifacts from settled, point-in-time rows."""

    def __init__(self, store, provider=None) -> None:
        self.store = store
        self.provider = provider

    def build(
        self,
        strategy_name: str,
        *,
        min_train_days: int = 60,
        top_k: int = 5,
        bootstrap_repeats: int = 1000,
    ) -> Dict[str, object]:
        strategy = storage_strategy_name(strategy_name)
        if self.provider is None:
            return {
                "ok": False,
                "status": "candidate_outcome_provider_unavailable",
                "strategy": strategy,
                "production_applied": False,
            }
        samples, coverage = self._load_samples(strategy, max(1, int(top_k)))
        dates = sorted((coverage.get("by_date") or {}).keys())
        if len(dates) <= max(5, int(min_train_days)):
            return {
                "ok": False,
                "status": "insufficient_real_oos_days",
                "strategy": strategy,
                "sample_count": int(coverage.get("eligible") or 0),
                "coverage_days": 0,
                "settled_candidate_days_available": len(dates),
                "required_train_days": int(min_train_days),
                "required_oos_days": 60,
                "production_applied": False,
            }

        oos = self._walk_forward(samples, dates, max(5, int(min_train_days)))
        oos_dates = dates[max(5, int(min_train_days)) :]
        oos_coverage = _coverage_for_dates(coverage.get("by_date") or {}, oos_dates)
        daily = self._counterfactual_days(strategy, oos, max(1, int(top_k)))
        increments = [
            coerce_number((item.get("outcome") or {}).get("deepseek_ablation_increment"))
            for item in daily
        ]
        production_increments = [coerce_number(item.get("incremental_net_return")) for item in daily]
        ci_low, ci_high = _moving_block_ci(increments, repeats=max(100, int(bootstrap_repeats)))
        local_max_drawdown = min([coerce_number(item.get("local_max_drawdown")) for item in daily] or [0.0])
        challenger_max_drawdown = min(
            [coerce_number(item.get("challenger_max_drawdown")) for item in daily] or [0.0]
        )
        regimes = {str(item.get("regime") or "unknown") for item in oos if str(item.get("regime") or "unknown") != "unknown"}
        regime_concentration = _increment_concentration(
            (
                str((item.get("outcome") or {}).get("regime") or "unknown"),
                coerce_number((item.get("outcome") or {}).get("deepseek_ablation_increment")),
            )
            for item in daily
        )
        industry_concentration = _increment_concentration(
            (industry, contribution)
            for item in daily
            for industry, contribution in ((item.get("outcome") or {}).get("industry_increment") or {}).items()
        )
        diversification_complete = bool(
            regime_concentration.get("positive_group_count", 0) >= 2
            and industry_concentration.get("positive_group_count", 0) >= 2
            and coerce_number(regime_concentration.get("max_share_pct"), 100.0) <= 80.0
            and coerce_number(industry_concentration.get("max_share_pct"), 100.0) <= 70.0
        )
        feature_coverage_pct = round(
            oos_coverage["non_abstain"] * 100.0 / max(1, oos_coverage["eligible"]),
            4,
        )
        validation = {
            "training_day_count": max(5, int(min_train_days)),
            "training_sample_count": sum(
                int((coverage.get("by_date") or {}).get(day, {}).get("eligible") or 0)
                for day in dates[: max(5, int(min_train_days))]
            ),
            "oos_day_count": len(oos_dates),
            "oos_settled_counterfactual_day_count": len(daily),
            "oos_sample_count": len(oos),
            "oos_eligible_candidate_count": oos_coverage["eligible"],
            "feature_coverage_pct": feature_coverage_pct,
            "feature_record_coverage_pct": round(
                oos_coverage["matched"] * 100.0 / max(1, oos_coverage["eligible"]),
                4,
            ),
            "incremental_return_mean": round(float(np.mean(increments)), 6) if increments else 0.0,
            "incremental_return_total": round(sum(increments), 6),
            "incremental_return_ci95_low": round(ci_low, 6),
            "incremental_return_ci95_high": round(ci_high, 6),
            "vs_production_incremental_return_total": round(sum(production_increments), 6),
            "local_max_drawdown_pct": round(local_max_drawdown, 6),
            "challenger_max_drawdown_pct": round(challenger_max_drawdown, 6),
            "max_drawdown_not_worse": challenger_max_drawdown >= local_max_drawdown,
            "regimes": sorted(regimes),
            "regime_coverage_complete": len(regimes) >= 2,
            "regime_increment_concentration": regime_concentration,
            "industry_increment_concentration": industry_concentration,
            "increment_diversification_complete": diversification_complete,
            "data_scope": "settled_full_frozen_candidate_pool_only",
        }
        coefficients, intercept, return_coefficients, return_intercept = _fit_models(samples)
        artifact = build_meta_artifact(
            strategy,
            coefficients=coefficients,
            intercept=intercept,
            return_coefficients=return_coefficients,
            return_intercept=return_intercept,
            trained_through=dates[-1],
            sample_count=oos_coverage["eligible"],
            coverage_days=len(oos_dates),
            validation=validation,
        )
        path = save_meta_artifact(artifact)
        return {
            "ok": True,
            "status": "shadow_artifact_written",
            "strategy": strategy,
            "artifact_id": artifact["artifact_id"],
            "artifact_path": path,
            "sample_count": oos_coverage["eligible"],
            "training_sample_count": len(samples) - len(oos),
            "coverage_days": len(oos_dates),
            "validation": validation,
            "promotion_gates": artifact["promotion_gates"],
            "production_applied": False,
        }

    def _load_samples(self, strategy: str, top_k: int) -> Tuple[List[Dict[str, object]], Dict[str, object]]:
        with self.store.repository.connect() as conn:
            conn.row_factory = sqlite3.Row
            date_rows = conn.execute(
                """
                SELECT DISTINCT c.signal_date
                FROM strategy_candidate_snapshots c
                JOIN strategy_signal_batches b
                  ON b.strategy_name = c.strategy_name
                 AND b.strategy_version = c.strategy_version
                 AND b.signal_date = c.signal_date
                 AND b.snapshot_phase = c.snapshot_phase
                WHERE c.strategy_name = ?
                  AND c.eligible = 1
                  AND c.point_in_time_valid = 1
                  AND lower(c.strategy_version) NOT LIKE '%replay%'
                ORDER BY c.signal_date ASC
                """,
                (strategy,),
            ).fetchall()
            features = conn.execute(
                """
                SELECT f.code, f.cutoff_at, f.completed_at, f.feature_json
                FROM deepseek_candidate_features f
                JOIN deepseek_analysis_batches b ON b.batch_id = f.batch_id
                WHERE f.strategy_name = ?
                  AND f.prompt_version = ?
                  AND f.feature_schema_version = ?
                  AND f.model_name = ?
                  AND f.valid = 1
                  AND b.model_tier = 'flash'
                  AND b.status IN ('ok', 'partial', 'cache_hit', 'no_evidence')
                ORDER BY f.completed_at ASC, f.id ASC
                """,
                (
                    strategy,
                    prompt_version(strategy),
                    FEATURE_SCHEMA_VERSION,
                    str(getattr(config, "DEEPSEEK_FEATURE_MODEL", "deepseek-v4-flash")),
                ),
            ).fetchall()
        by_code_date: Dict[tuple, List[Dict[str, object]]] = defaultdict(list)
        for raw in features:
            item = dict(raw)
            try:
                item["feature"] = json.loads(item.pop("feature_json") or "{}")
            except Exception:
                continue
            if not isinstance(item.get("feature"), dict):
                continue
            key = (normalize_code(item.get("code")), str(item.get("cutoff_at") or "")[:10])
            by_code_date[key].append(item)
        samples: List[Dict[str, object]] = []
        coverage: Dict[str, object] = {
            "eligible": 0,
            "matched": 0,
            "non_abstain": 0,
            "by_date": {},
        }
        portfolio_service = DailyPortfolioBaselineService(self.store)
        target_weight_pct = 100.0 / max(1, int(top_k))
        for date_row in date_rows:
            signal_date = str(date_row[0] or "")
            dataset = portfolio_service.candidate_execution_dataset(
                self.provider,
                strategy,
                signal_date,
            )
            if dataset.get("status") != "settled":
                continue
            candidates = list(dataset.get("candidates") or [])
            if not candidates:
                continue
            signal_time = str(dataset.get("signal_time") or "")
            policy = dict(dataset.get("execution_policy") or {})
            outcomes = dict(dataset.get("outcomes") or {})
            day_coverage = {"eligible": 0, "matched": 0, "non_abstain": 0}
            for candidate in candidates:
                code = normalize_code(candidate.get("code"))
                outcome = outcomes.get(code) or {}
                status = str(outcome.get("status") or "unknown")
                if status not in {"settled", "unfilled"}:
                    continue
                day_coverage["eligible"] += 1
                key = (code, signal_date[:10])
                eligible_features = [
                    item
                    for item in by_code_date.get(key, [])
                    if str(item.get("cutoff_at") or "") <= signal_time
                    and str(item.get("completed_at") or "") <= signal_time
                ]
                if eligible_features:
                    feature = dict(eligible_features[-1]["feature"])
                    day_coverage["matched"] += 1
                    if not feature.get("abstain"):
                        day_coverage["non_abstain"] += 1
                else:
                    feature = abstain_feature(candidate, strategy, "missing_precomputed_feature")
                    feature["deepseek_missing"] = True
                row = dict(candidate)
                row.update(
                    code=code,
                    score=coerce_number(candidate.get("score"), 50.0),
                    local_score=coerce_number(candidate.get("score"), 50.0),
                )
                if status == "unfilled":
                    net_return = 0.0
                else:
                    weighted = dict(candidate)
                    weighted["suggested_weight"] = target_weight_pct
                    trade_cost = coerce_number(
                        (cost_scenarios(weighted, policy).get("base") or {}).get("total_pct")
                    )
                    net_return = coerce_number(outcome.get("gross_return_pct")) - trade_cost
                samples.append(
                    {
                        "signal_date": signal_date,
                        "strategy_version": str(dataset.get("strategy_version") or ""),
                        "code": code,
                        "production_selected": bool(
                            candidate.get("selected")
                            if strategy == "short_term"
                            else candidate.get("current_rule_selected")
                        ),
                        "local_score": row["local_score"],
                        "net_return": round(net_return, 6),
                        "positive": 1.0 if net_return > 0 else 0.0,
                        "regime": str(row.get("market_regime") or row.get("regime") or "unknown"),
                        "industry": str(row.get("industry") or row.get("theme") or "unknown"),
                        "vector": meta_feature_vector(row, feature),
                    }
                )
            if day_coverage["eligible"]:
                coverage["by_date"][signal_date] = day_coverage
                for key in ("eligible", "matched", "non_abstain"):
                    coverage[key] += day_coverage[key]
        return samples, coverage

    @staticmethod
    def _walk_forward(
        samples: List[Dict[str, object]],
        dates: List[str],
        min_train_days: int,
    ) -> List[Dict[str, object]]:
        result: List[Dict[str, object]] = []
        refit_every = 5
        model = None
        ablation_model = None
        for index, test_date in enumerate(dates[min_train_days:], start=min_train_days):
            if model is None or (index - min_train_days) % refit_every == 0:
                train = [item for item in samples if item["signal_date"] < test_date]
                model = _fit_models(train)
                ablation_model = _fit_models(train, feature_names=LOCAL_ABLATION_FEATURES)
            coefficients, intercept, return_coefficients, return_intercept = model
            (
                ablation_coefficients,
                ablation_intercept,
                ablation_return_coefficients,
                ablation_return_intercept,
            ) = ablation_model
            for item in samples:
                if item["signal_date"] != test_date:
                    continue
                next_item = dict(item)
                probability = _predict(item["vector"], coefficients, intercept, logistic=True)
                expected_return = _predict(item["vector"], return_coefficients, return_intercept, logistic=False)
                ablation_probability = _predict(
                    item["vector"],
                    ablation_coefficients,
                    ablation_intercept,
                    logistic=True,
                    feature_names=LOCAL_ABLATION_FEATURES,
                )
                ablation_expected_return = _predict(
                    item["vector"],
                    ablation_return_coefficients,
                    ablation_return_intercept,
                    logistic=False,
                    feature_names=LOCAL_ABLATION_FEATURES,
                )
                next_item["shadow_probability"] = probability
                next_item["shadow_expected_return"] = expected_return
                next_item["shadow_rank_key"] = probability * expected_return
                next_item["ablation_rank_key"] = ablation_probability * ablation_expected_return
                result.append(next_item)
        return result

    def _counterfactual_days(
        self,
        strategy: str,
        oos: List[Dict[str, object]],
        top_k: int,
    ) -> List[Dict[str, object]]:
        grouped: Dict[str, List[Dict[str, object]]] = defaultdict(list)
        for item in oos:
            grouped[item["signal_date"]].append(item)
        result = []
        for signal_date, items in sorted(grouped.items()):
            local = sorted(
                (item for item in items if item.get("production_selected")),
                key=lambda item: (-coerce_number(item.get("local_score")), item["code"]),
            )[:top_k]
            ablation = sorted(
                items,
                key=lambda item: (-coerce_number(item.get("ablation_rank_key")), item["code"]),
            )[:top_k]
            challenger = sorted(items, key=lambda item: (-coerce_number(item.get("shadow_rank_key")), item["code"]))[:top_k]
            local_return = _top_k_return(local, top_k)
            ablation_return = _top_k_return(ablation, top_k)
            challenger_return = _top_k_return(challenger, top_k)
            row = {
                "strategy_name": strategy,
                "signal_date": signal_date,
                "strategy_version": str(items[0].get("strategy_version") or ""),
                "prompt_version": prompt_version(strategy),
                "model_name": "deepseek_meta_linear_v2",
                "local_codes": [item["code"] for item in local],
                "challenger_codes": [item["code"] for item in challenger],
                "replacements": sorted(set(item["code"] for item in challenger) - set(item["code"] for item in local)),
                "local_net_return": local_return,
                "challenger_net_return": challenger_return,
                "incremental_net_return": round(challenger_return - local_return, 6),
                "status": "settled_shadow",
                "outcome": {
                    "candidate_count": len(items),
                    "top_k": top_k,
                    "regime": str(items[0].get("regime") or "unknown"),
                    "ablation_local_codes": [item["code"] for item in ablation],
                    "ablation_local_return": ablation_return,
                    "deepseek_ablation_increment": round(challenger_return - ablation_return, 6),
                    "industry_increment": _industry_increment(ablation, challenger, top_k),
                },
            }
            result.append(row)
        local_drawdowns = _drawdown_path([item["local_net_return"] for item in result])
        challenger_drawdowns = _drawdown_path([item["challenger_net_return"] for item in result])
        for index, row in enumerate(result):
            row["local_max_drawdown"] = local_drawdowns[index]
            row["challenger_max_drawdown"] = challenger_drawdowns[index]
        self.store.save_deepseek_counterfactual_outcomes(result)
        return result



def _matrix(samples: Iterable[Dict[str, object]], feature_names=META_FEATURES):
    rows = list(samples or [])
    names = tuple(feature_names or META_FEATURES)
    x = np.asarray([[coerce_number(item["vector"].get(name)) for name in names] for item in rows], dtype=float)
    y_binary = np.asarray([coerce_number(item.get("positive")) for item in rows], dtype=float)
    y_return = np.asarray([coerce_number(item.get("net_return")) for item in rows], dtype=float)
    return x, y_binary, y_return


def _fit_models(samples: Iterable[Dict[str, object]], feature_names=META_FEATURES):
    rows = list(samples or [])
    names = tuple(feature_names or META_FEATURES)
    if not rows:
        zeros = {name: 0.0 for name in names}
        return zeros, 0.0, zeros, 0.0
    x, y_binary, y_return = _matrix(rows, names)
    design = np.column_stack([np.ones(len(x)), x])
    ridge = np.eye(design.shape[1]) * 0.01
    ridge[0, 0] = 0.0
    return_beta = np.linalg.pinv(design.T @ design + ridge) @ design.T @ y_return
    beta = np.zeros(design.shape[1], dtype=float)
    learning_rate = 0.08
    for _ in range(500):
        logits = np.clip(design @ beta, -30.0, 30.0)
        predicted = 1.0 / (1.0 + np.exp(-logits))
        gradient = design.T @ (predicted - y_binary) / max(1, len(x))
        gradient[1:] += 0.01 * beta[1:]
        beta -= learning_rate * gradient
    coefficients = {name: float(beta[index + 1]) for index, name in enumerate(names)}
    return_coefficients = {
        name: float(return_beta[index + 1]) for index, name in enumerate(names)
    }
    return coefficients, float(beta[0]), return_coefficients, float(return_beta[0])


def _predict(
    vector: Dict[str, float],
    coefficients: Dict[str, float],
    intercept: float,
    *,
    logistic: bool,
    feature_names=META_FEATURES,
) -> float:
    value = coerce_number(intercept)
    for name in tuple(feature_names or META_FEATURES):
        value += coerce_number(coefficients.get(name)) * coerce_number(vector.get(name))
    if logistic:
        return 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, value))))
    return value


def _moving_block_ci(values: List[float], *, repeats: int = 1000, block_size: int = 5) -> Tuple[float, float]:
    if not values:
        return 0.0, 0.0
    if len(values) == 1:
        return values[0], values[0]
    rng = random.Random(20260713)
    blocks = [values[index : index + block_size] for index in range(0, len(values))]
    estimates = []
    for _ in range(repeats):
        sampled: List[float] = []
        while len(sampled) < len(values):
            sampled.extend(rng.choice(blocks))
        estimates.append(_mean(sampled[: len(values)]))
    estimates.sort()
    low = estimates[max(0, int(len(estimates) * 0.025) - 1)]
    high = estimates[min(len(estimates) - 1, int(len(estimates) * 0.975))]
    return low, high


def _drawdown_path(returns_pct: Iterable[float]) -> List[float]:
    equity = 1.0
    peak = 1.0
    worst = 0.0
    result = []
    for value in returns_pct:
        equity *= 1.0 + coerce_number(value) / 100.0
        peak = max(peak, equity)
        drawdown = (equity / peak - 1.0) * 100.0 if peak > 0 else 0.0
        worst = min(worst, drawdown)
        result.append(round(worst, 6))
    return result


def _mean(values: Iterable[float]) -> float:
    items = [coerce_number(value) for value in values]
    return round(sum(items) / max(1, len(items)), 6)


def _top_k_return(rows: Iterable[Dict[str, object]], top_k: int) -> float:
    return round(
        sum(coerce_number(item.get("net_return")) for item in rows or [])
        / max(1, int(top_k)),
        6,
    )


def _coverage_for_dates(by_date: Dict[str, Dict[str, int]], dates: Iterable[str]) -> Dict[str, int]:
    result = {"eligible": 0, "matched": 0, "non_abstain": 0}
    for date_value in dates or []:
        item = by_date.get(str(date_value), {}) if isinstance(by_date, dict) else {}
        for key in result:
            result[key] += int(item.get(key) or 0)
    return result


def _industry_increment(
    local: List[Dict[str, object]],
    challenger: List[Dict[str, object]],
    top_k: int,
) -> Dict[str, float]:
    local_codes = {str(item.get("code") or "") for item in local}
    challenger_codes = {str(item.get("code") or "") for item in challenger}
    contributions: Dict[str, float] = defaultdict(float)
    divisor = max(1, int(top_k))
    for item in challenger:
        if str(item.get("code") or "") not in local_codes:
            contributions[str(item.get("industry") or "unknown")] += coerce_number(item.get("net_return")) / divisor
    for item in local:
        if str(item.get("code") or "") not in challenger_codes:
            contributions[str(item.get("industry") or "unknown")] -= coerce_number(item.get("net_return")) / divisor
    return {key: round(value, 6) for key, value in sorted(contributions.items())}


def _increment_concentration(pairs: Iterable[Tuple[str, float]]) -> Dict[str, object]:
    grouped: Dict[str, float] = defaultdict(float)
    for group, value in pairs:
        grouped[str(group or "unknown")] += coerce_number(value)
    positive = {key: value for key, value in grouped.items() if value > 0}
    total = sum(positive.values())
    max_share = max(positive.values()) * 100.0 / total if total > 0 else 100.0
    return {
        "positive_group_count": len(positive),
        "max_share_pct": round(max_share, 4),
        "positive_increment_total": round(total, 6),
        "groups": {key: round(value, 6) for key, value in sorted(grouped.items())},
    }
