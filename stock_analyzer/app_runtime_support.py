from typing import Dict, List, Tuple

from . import config
from .deepseek_client import rerank_candidates, rerank_candidates_batch, review_strategy_validation
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
    try:
        return rerank_candidates(rows=rows, strategy_name=strategy_name, market_filter=market_filter)
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
            active[strategy] = rows
    if not active:
        return rows_by_strategy, meta
    try:
        reranked, batch_meta = rerank_candidates_batch(active, market_filter=market_filter)
        result_rows = dict(rows_by_strategy)
        result_rows.update(reranked)
        meta.update(batch_meta)
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
    meta["deepseek_filtered_count"] = int(deepseek_meta.get("filtered") or 0)
    if deepseek_meta.get("filter_reasons"):
        meta["deepseek_filter_reasons"] = deepseek_meta.get("filter_reasons")


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
