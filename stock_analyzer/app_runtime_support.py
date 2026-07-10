from typing import Dict, List, Tuple

from . import config
from .deepseek_client import rerank_candidates, rerank_candidates_batch, review_strategy_validation
from .deepseek_scheduler import (
    reuse_scheduled_deepseek_result,
    save_scheduled_deepseek_result,
    scheduled_deepseek_decision,
)
from .normalization import normalize_code
from .stock_optimization import review_stock_prediction
from .strategies import storage_strategy_name


def risk_blacklist_summary(payload: Dict[str, object]) -> Dict[str, object]:
    payload = payload or {}
    return {
        "enabled": bool(getattr(config, "ENABLE_RISK_BLACKLIST", True)),
        "hard_filter": bool(getattr(config, "RISK_BLACKLIST_HARD_FILTER", True)),
        "status": payload.get("status", "missing"),
        "item_count": len((payload.get("items") or {})),
        "sources": payload.get("sources", []),
        "error_count": len(payload.get("errors") or []),
    }


def deepseek_rerank_disabled_strategies() -> set:
    raw = str(getattr(config, "DEEPSEEK_RERANK_DISABLED_STRATEGIES", "") or "").strip()
    if not raw:
        return set()
    return {item.strip() for item in raw.replace("，", ",").split(",") if item.strip()}


def apply_deepseek_rerank(
    strategy_name: str,
    rows: List[Dict[str, object]],
    market_filter: str,
) -> Tuple[List[Dict[str, object]], Dict[str, object]]:
    if not rows:
        return rows, {"enabled": False, "status": "empty"}
    if not getattr(config, "ENABLE_DEEPSEEK_RUNTIME", False):
        return rows, {
            "enabled": False,
            "status": "runtime_disabled",
            "strategy": strategy_name,
            "reason": "DeepSeek runtime is disabled; local rules are used only.",
        }
    disabled_strategies = deepseek_rerank_disabled_strategies()
    if strategy_name in disabled_strategies or "all" in disabled_strategies:
        return rows, {
            "enabled": False,
            "status": "strategy_rerank_disabled",
            "strategy": strategy_name,
            "reason": "DeepSeek rerank is disabled for this strategy route.",
        }
    decision = scheduled_deepseek_decision(strategy_name, rows)
    if decision.get("enabled") and not decision.get("allow_call"):
        return reuse_scheduled_deepseek_result(strategy_name, rows, decision)
    try:
        reranked, meta = rerank_candidates(
            rows=rows,
            strategy_name=strategy_name,
            market_filter=market_filter,
            model_tier_override=str(decision.get("model_tier") or ""),
            review_limit_override=int(decision.get("review_limit") or 0),
        )
        if decision.get("enabled"):
            meta["schedule"] = {
                "status": decision.get("status", ""),
                "slot": decision.get("slot", ""),
                "model_tier": decision.get("model_tier", ""),
                "reused": False,
            }
            save_scheduled_deepseek_result(strategy_name, reranked, meta, decision)
        return reranked, meta
    except Exception as exc:
        return rows, {
            "enabled": False,
            "status": "fallback",
            "strategy": strategy_name,
            "error": str(exc),
        }


def apply_deepseek_rerank_batch(
    rows_by_strategy: Dict[str, List[Dict[str, object]]],
    market_filter: str,
) -> Tuple[Dict[str, List[Dict[str, object]]], Dict[str, Dict[str, object]]]:
    rows_by_strategy = {
        storage_strategy_name(strategy): list(rows or [])
        for strategy, rows in (rows_by_strategy or {}).items()
    }
    if not getattr(config, "ENABLE_DEEPSEEK_RUNTIME", False):
        return rows_by_strategy, {
            strategy: {
                "enabled": False,
                "status": "runtime_disabled",
                "strategy": strategy,
                "reason": "DeepSeek runtime is disabled; local rules are used only.",
            }
            for strategy in rows_by_strategy
        }
    disabled_strategies = deepseek_rerank_disabled_strategies()
    active = {}
    meta = {}
    schedule_decisions = {}
    result_rows = dict(rows_by_strategy)
    for strategy, rows in rows_by_strategy.items():
        if not rows:
            meta[strategy] = {"enabled": False, "status": "empty", "strategy": strategy}
        elif strategy in disabled_strategies or "all" in disabled_strategies:
            meta[strategy] = {
                "enabled": False,
                "status": "strategy_rerank_disabled",
                "strategy": strategy,
                "reason": "DeepSeek rerank is disabled for this strategy route.",
            }
        else:
            decision = scheduled_deepseek_decision(strategy, rows)
            schedule_decisions[strategy] = decision
            if decision.get("enabled") and not decision.get("allow_call"):
                reused_rows, reused_meta = reuse_scheduled_deepseek_result(strategy, rows, decision)
                result_rows[strategy] = reused_rows
                meta[strategy] = reused_meta
            else:
                active[strategy] = rows
    if not active:
        return result_rows, meta
    try:
        scheduled_decisions = [
            decision for strategy, decision in schedule_decisions.items() if strategy in active and decision.get("enabled")
        ]
        model_tier = str(scheduled_decisions[0].get("model_tier") or "") if len(scheduled_decisions) == 1 else ""
        review_limit = int(scheduled_decisions[0].get("review_limit") or 0) if len(scheduled_decisions) == 1 else 0
        reranked, batch_meta = rerank_candidates_batch(
            active,
            market_filter=market_filter,
            model_tier_override=model_tier,
            review_limit_override=review_limit,
        )
        result_rows.update(reranked)
        meta.update(batch_meta)
        for strategy in active:
            decision = schedule_decisions.get(strategy) or {}
            if not decision.get("enabled"):
                continue
            strategy_meta = meta.setdefault(strategy, {})
            strategy_meta["schedule"] = {
                "status": decision.get("status", ""),
                "slot": decision.get("slot", ""),
                "model_tier": decision.get("model_tier", ""),
                "reused": False,
            }
            save_scheduled_deepseek_result(
                strategy,
                result_rows.get(strategy, []),
                strategy_meta,
                decision,
            )
        return result_rows, meta
    except Exception as exc:
        for strategy in active:
            meta[strategy] = {
                "enabled": False,
                "status": "fallback",
                "strategy": strategy,
                "source": "deepseek_batch",
                "error": str(exc),
            }
        return rows_by_strategy, meta


def skipped_deepseek_meta(
    strategy_name: str,
    *,
    reason: str = "DeepSeek rerank is deferred to background refresh.",
    status: str = "async_pending",
) -> Dict[str, object]:
    return {
        "enabled": bool(getattr(config, "ENABLE_DEEPSEEK_RUNTIME", False)),
        "status": status,
        "strategy": strategy_name,
        "reason": reason,
    }


def finalize_deepseek_meta(
    meta: Dict[str, object],
    rows: List[Dict[str, object]],
    deepseek_meta: Dict[str, object],
) -> None:
    meta["deepseek"] = _public_deepseek_meta(deepseek_meta)
    meta["display_count"] = len(rows)
    sync_tomorrow_tier_meta(meta, rows)
    meta["deepseek_filtered_count"] = int(deepseek_meta.get("filtered") or 0)
    if deepseek_meta.get("filter_reasons"):
        meta["deepseek_filter_reasons"] = deepseek_meta.get("filter_reasons")


def sync_tomorrow_tier_meta(
    meta: Dict[str, object],
    rows: List[Dict[str, object]],
) -> None:
    if not isinstance(meta, dict):
        return
    strategy_version = str(meta.get("strategy_version") or "")
    if not strategy_version.startswith("tomorrow_picks") and meta.get("strategy_label") not in {
        "明日优先",
        "明天推荐",
    }:
        return
    primary_count = sum(1 for row in rows or [] if row.get("tier") == "primary_watch")
    meta["display_count"] = len(rows or [])
    meta["primary_watch_count"] = primary_count
    meta["backup_watch_count"] = max(0, len(rows or []) - primary_count)


def _public_deepseek_meta(deepseek_meta: Dict[str, object]) -> Dict[str, object]:
    item = dict(deepseek_meta or {})
    item.pop("filtered_rows", None)
    return item


def attach_factor_snapshots(samples: List[Dict[str, object]]) -> List[Dict[str, object]]:
    if not samples:
        return samples
    from .factor_snapshot import FactorSnapshotStore

    try:
        store = FactorSnapshotStore(config.FACTOR_SNAPSHOT_DB_PATH)
        lookup = store.lookup(samples)
    except Exception:
        return samples
    if not lookup:
        return samples
    enriched = []
    for sample in samples:
        item = dict(sample)
        signal_date = str(item.get("signal_date") or "").strip()[:10].replace("-", "")
        code = normalize_code(item.get("code"))
        factors = lookup.get((signal_date, code))
        if factors:
            item["factor_snapshot"] = factors
        enriched.append(item)
    return enriched


def deepseek_validation_review(
    validation_store,
    strategy_name: str,
    metrics: Dict[str, object],
    days: int,
) -> Dict[str, object]:
    if not getattr(config, "ENABLE_DEEPSEEK_RUNTIME", False):
        return {
            "enabled": False,
            "status": "runtime_disabled",
            "strategy": strategy_name,
            "reason": "DeepSeek runtime is disabled; validation uses local metrics only.",
        }
    min_new_days = max(1, int(getattr(config, "DEEPSEEK_VALIDATION_REVIEW_MIN_NEW_DAYS", 5)))
    current_real_days = int(metrics.get("real_day_count") or 0)
    last_review_days = _latest_completed_deepseek_review_days(validation_store, strategy_name)
    if current_real_days < min_new_days or current_real_days - last_review_days < min_new_days:
        return {
            "enabled": False,
            "status": "cadence_deferred",
            "strategy": strategy_name,
            "real_day_count": current_real_days,
            "last_review_real_day_count": last_review_days,
            "min_new_real_days": min_new_days,
            "reason": "真实验证样本尚未新增足够交易日，复用本地门控并暂缓 DeepSeek 盘后复盘。",
        }
    try:
        samples = validation_store.live_weight_samples(strategy_name, days=max(20, min(days, 60)))
        samples = attach_factor_snapshots(samples)
        return review_strategy_validation(
            strategy_name=strategy_name,
            metrics=metrics,
            samples=samples,
            days=days,
        )
    except Exception as exc:
        return {
            "enabled": False,
            "status": "fallback",
            "strategy": strategy_name,
            "error": str(exc),
        }


def _latest_completed_deepseek_review_days(validation_store, strategy_name: str) -> int:
    try:
        runs = validation_store.list_tuning_runs(strategy_name, limit=30)
    except Exception:
        return 0
    for run in runs or []:
        review = run.get("deepseek") if isinstance(run, dict) else {}
        if not isinstance(review, dict):
            continue
        if review.get("status") not in {"ok", "cache_hit"}:
            continue
        metrics = run.get("metrics") if isinstance(run.get("metrics"), dict) else {}
        return int(metrics.get("real_day_count") or 0)
    return 0


def deepseek_stock_prediction_review(
    prediction_payload: Dict[str, object],
) -> Dict[str, object]:
    if not getattr(config, "ENABLE_DEEPSEEK_RUNTIME", False):
        return {
            "enabled": False,
            "status": "runtime_disabled",
            "strategy": _primary_prediction_strategy(prediction_payload),
            "reason": "DeepSeek runtime is disabled; stock prediction uses local rules only.",
        }
    try:
        return review_stock_prediction(
            prediction_payload,
            strategy_name=_primary_prediction_strategy(prediction_payload),
        )
    except Exception as exc:
        return {
            "enabled": False,
            "status": "fallback",
            "strategy": _primary_prediction_strategy(prediction_payload),
            "error": str(exc),
        }


def _primary_prediction_strategy(prediction_payload: Dict[str, object]) -> str:
    strategy_hits = (prediction_payload or {}).get("strategy_hits") or []
    if strategy_hits:
        first_hit = strategy_hits[0] or {}
        return storage_strategy_name(str(first_hit.get("strategy_name") or "short_term"))
    return "short_term"
