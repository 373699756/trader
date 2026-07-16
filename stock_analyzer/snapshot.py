from datetime import datetime
from typing import Dict, Iterable, List, Optional
from uuid import uuid4

import pandas as pd

from . import config
from .app_runtime_support import finalize_deepseek_meta
from .daily_data import load_history_frames
from .deepseek.runtime_features import attach_persisted_deepseek_features
from .deepseek.production_merge import (
    attach_and_merge_rows,
    deepseek_meta_for_rows,
    ranking_groups,
)
from .event_risk import attach_event_risk, load_event_risk
from .execution_policy import build_execution_policy
from .factors import build_alphalite_factors, merge_alphalite
from .fundamentals import attach_fundamental_factors, load_fundamentals
from .normalization import coerce_number, normalize_code
from .long_term_watch import LongTermWatchScorer
from .pit_snapshot import CLOSE_FORWARD, PointInTimeSnapshotStore
from .point_in_time import (
    build_candidate_snapshot_rows,
    filter_point_in_time_events,
    filter_point_in_time_fundamentals,
)
from .production_baseline import attach_generation_provenance
from .scoring_core.candidate_filters import prepare_candidates
from .scoring_core.market_regime import build_market_regime
from .strategies import score_swing_2_5d_picks, score_today_picks, score_tomorrow_picks, storage_strategy_name
from .snapshot_phase import (
    CLOSE_FALLBACK,
    PRECLOSE_TRADEABLE,
    close_quote_is_valid,
    market_close_reached,
    normalize_snapshot_phase,
    phase_payload,
)
from .validation_repository import SignalFreezeDeadlineExceeded
from .validation_policy import current_strategy_version


SNAPSHOT_STRATEGIES = tuple(config.SNAPSHOT_STRATEGIES)
SNAPSHOT_REQUIRED_FACTOR_FIELDS = ("volume_ratio", "turnover_rate", "amplitude")
SNAPSHOT_SAMPLE_TYPE = "real_forward"
SNAPSHOT_SAMPLE_SOURCE = "intraday_pit_14_30"
CLOSE_SNAPSHOT_SAMPLE_TYPE = CLOSE_FORWARD
CLOSE_SNAPSHOT_SAMPLE_SOURCE = "official_close_fallback"


def run_snapshot(
    provider,
    validation_store,
    strategy: str,
    market: str = "all",
    _context: Dict[str, object] = None,
    snapshot_phase: str = PRECLOSE_TRADEABLE,
) -> Dict[str, object]:
    strategy = storage_strategy_name(strategy)
    snapshot_phase = normalize_snapshot_phase(snapshot_phase, PRECLOSE_TRADEABLE)
    close_fallback = snapshot_phase == CLOSE_FALLBACK
    sample_type = CLOSE_SNAPSHOT_SAMPLE_TYPE if close_fallback else SNAPSHOT_SAMPLE_TYPE
    sample_source = CLOSE_SNAPSHOT_SAMPLE_SOURCE if close_fallback else SNAPSHOT_SAMPLE_SOURCE
    if strategy not in SNAPSHOT_STRATEGIES:
        return {"ok": False, "strategy": strategy, "error": "unknown_strategy"}
    context = _context or _prepare_snapshot_context(provider)
    if context.get("error"):
        common_signal_time = str(context.get("snapshot_cutoff") or datetime.now().isoformat(timespec="seconds"))
        snapshot_id = str(context.get("snapshot_id") or "snapshot_{}".format(uuid4().hex))
        return {
            "ok": False,
            "strategy": strategy,
            "error": str(context["error"]),
            "saved": {"saved": 0, "replaced": 0},
            "meta": {
                **phase_payload(snapshot_phase, as_of=common_signal_time),
                "generated_at": common_signal_time,
                "snapshot_id": snapshot_id,
                "rejection": "snapshot_context_rejected",
            },
        }

    quotes = context["quotes"]
    snapshot_cutoff = str(context["snapshot_cutoff"])
    snapshot_id = str(context["snapshot_id"])
    event_payload = context["event_payload"]
    fundamental_payload = context["fundamental_payload"]
    candidates = context["candidates"].copy(deep=True)
    market_regime = dict(context["market_regime"])
    common_signal_time = str(context.get("signal_time") or snapshot_cutoff)
    context["signal_time"] = common_signal_time
    if not close_fallback and _signal_at_or_after_freeze_cutoff(common_signal_time):
        return _freeze_rejection(
            strategy,
            {"generated_at": common_signal_time, "snapshot_id": snapshot_id},
        )
    try:
        quote_timestamp = str((getattr(quotes, "attrs", {}) or {}).get("quote_timestamp") or "")
        if close_fallback and not close_quote_is_valid(
            quote_timestamp,
            signal_date=common_signal_time[:10],
            close_time=str(getattr(config, "MARKET_CLOSE_TIME", "15:00")),
        ):
            return {
                "ok": False,
                "strategy": strategy,
                "error": "收盘行情时间戳无效，拒绝生成盘后补充样本。",
                "saved": {"saved": 0, "replaced": 0},
                "meta": {
                    **phase_payload(snapshot_phase, as_of=common_signal_time),
                    "generated_at": common_signal_time,
                    "snapshot_id": snapshot_id,
                    "quote_timestamp": quote_timestamp,
                    "rejection": "close_quote_timestamp_invalid",
                },
            }
        pit_archive = _archive_point_in_time_quotes(
            validation_store,
            context,
            provider,
            sample_type=sample_type,
            sample_source=sample_source,
        )
    except Exception as exc:
        return _snapshot_error_rejection(
            strategy=strategy,
            provider=provider,
            validation_store=validation_store,
            snapshot_id=snapshot_id,
            signal_time=common_signal_time,
            candidates=candidates,
            quotes=quotes,
            event_payload=event_payload,
            fundamental_payload=fundamental_payload,
            error="原始全市场点时行情归档失败：{}".format(exc),
            market=market,
            market_regime=market_regime,
            generation_status="pit_archive_failed",
            reason="raw_market_snapshot_not_persisted",
            batch_metadata={
                "data_source_timestamp": "",
                "market_data_cutoff": common_signal_time,
                "snapshot_id": snapshot_id,
                "freeze_deadline": _freeze_deadline(common_signal_time),
                "sample_type": "unknown",
                "sample_source": "pit_archive_failed",
                "snapshot_phase": snapshot_phase,
            },
            snapshot_phase=snapshot_phase,
        )
    if str(pit_archive.get("sample_type") or "") != sample_type:
        return _snapshot_error_rejection(
            strategy=strategy,
            provider=provider,
            validation_store=validation_store,
            snapshot_id=snapshot_id,
            signal_time=common_signal_time,
            candidates=candidates,
            quotes=quotes,
            event_payload=event_payload,
            fundamental_payload=fundamental_payload,
            error=(
                "收盘行情缺少可信的15:00后来源时间，不能标记为收盘补充样本。"
                if close_fallback
                else "点时行情缺少 14:30-14:50 的可信来源时间，不能标记为真实前瞻样本。"
            ),
            market=market,
            market_regime=market_regime,
            generation_status="pit_timestamp_invalid",
            reason="raw_market_snapshot_not_real_forward",
            batch_metadata={
                "data_source_timestamp": str(pit_archive.get("data_source_timestamp") or ""),
                "market_data_cutoff": common_signal_time,
                "snapshot_id": snapshot_id,
                "freeze_deadline": _freeze_deadline(common_signal_time),
                "sample_type": str(pit_archive.get("sample_type") or "unknown"),
                "sample_source": sample_source,
                "snapshot_phase": snapshot_phase,
            },
            snapshot_phase=snapshot_phase,
        )
    freeze_deadline = "" if close_fallback else _freeze_deadline(common_signal_time)
    try:
        rows, meta, version = _score_snapshot_strategy(
            provider,
            candidates,
            quotes,
            strategy,
            market,
            market_regime,
            validation_store=validation_store,
            signal_time=common_signal_time,
        )
    except Exception as exc:
        return _snapshot_error_rejection(
            strategy=strategy,
            provider=provider,
            validation_store=validation_store,
            snapshot_id=snapshot_id,
            signal_time=common_signal_time,
            candidates=candidates,
            quotes=quotes,
            event_payload=event_payload,
            fundamental_payload=fundamental_payload,
            error=exc,
            market=market,
            market_regime=market_regime,
            batch_metadata={
                "data_source_timestamp": "",
                "market_data_cutoff": common_signal_time,
                "snapshot_id": snapshot_id,
                "freeze_deadline": freeze_deadline,
                "sample_type": sample_type,
                "sample_source": sample_source,
                "snapshot_phase": snapshot_phase,
            },
            snapshot_phase=snapshot_phase,
        )
    version = str(version or "").strip() or current_strategy_version(strategy)
    if not version:
        version = str(strategy)
    _attach_frozen_regime(rows, market_regime)
    for row in rows:
        row["snapshot_phase"] = snapshot_phase
        row["price_basis"] = "official_close" if close_fallback else "signal_time_quote"
    meta["generated_at"] = common_signal_time
    meta["snapshot_id"] = snapshot_id
    scored_candidate_rows = meta.pop("_candidate_pool_rows", None)
    _attach_frozen_regime(scored_candidate_rows, market_regime)
    rows = attach_and_merge_rows(
        rows,
        strategy,
        validation_store,
        signal_time=common_signal_time,
    )
    deepseek_meta = deepseek_meta_for_rows(rows, strategy)
    comparison_groups = ranking_groups(scored_candidate_rows or rows, top_k=5)
    deepseek_meta.update(
        source="validation_db_point_in_time_cache",
        ranking_groups=comparison_groups,
        reason="快照冻结本地、DeepSeek和75/25综合三组结果，API失败时综合组回退本地组。",
    )
    finalize_deepseek_meta(meta, rows, deepseek_meta)
    provider_health = _provider_health(provider)
    provenance = attach_generation_provenance(meta, strategy, rows, candidates)
    provenance["deepseek_ranking_groups"] = comparison_groups
    candidate_rows = build_candidate_snapshot_rows(
        quotes,
        candidates,
        rows,
        meta["generated_at"],
        scored_rows=scored_candidate_rows,
        event_payload=event_payload,
        fundamental_payload=fundamental_payload,
        provider_health=provider_health,
        strategy_name=strategy,
        snapshot_id=snapshot_id,
    )
    for row in candidate_rows:
        row["sample_type"] = sample_type
        row["sample_source"] = sample_source
        row["snapshot_phase"] = snapshot_phase
    execution_policy = build_execution_policy(strategy, market, snapshot_phase=snapshot_phase)
    data_source_timestamp = str(
        (quotes.attrs or {}).get("quote_timestamp")
        or provider_health.get("last_quote_refresh")
        or ""
    )
    meta["point_in_time"] = {
        "candidate_count": len(candidate_rows),
        "eligible_count": sum(1 for row in candidate_rows if row.get("eligible")),
        "selected_count": sum(1 for row in candidate_rows if row.get("selected")),
        "valid_count": sum(1 for row in candidate_rows if row.get("point_in_time_valid")),
        "market_data_cutoff": meta["generated_at"],
        "data_source_timestamp": data_source_timestamp,
        "fundamentals": fundamental_payload.get("point_in_time") or {},
        "events": event_payload.get("point_in_time") or {},
        "market_archive": pit_archive,
    }
    meta.update(phase_payload(snapshot_phase, as_of=common_signal_time))
    meta["execution_policy_version"] = execution_policy["policy_version"]
    freeze_ready_at = datetime.now().isoformat(timespec="seconds")
    meta["freeze_ready_at"] = freeze_ready_at
    if not close_fallback and _signal_at_or_after_freeze_cutoff(freeze_ready_at):
        return _freeze_rejection(strategy, meta)
    freeze_deadline = "" if close_fallback else _freeze_deadline(common_signal_time)
    batch_metadata = {
        "data_source_timestamp": "",
        "market_data_cutoff": meta["generated_at"],
        "snapshot_id": snapshot_id,
        "freeze_deadline": freeze_deadline,
        "sample_type": sample_type,
        "sample_source": sample_source,
        "snapshot_phase": snapshot_phase,
    }
    try:
        saved = validation_store.save_signals(
            strategy,
            version,
            meta["generated_at"],
            rows,
            candidate_rows=candidate_rows,
            batch_metadata={
                **batch_metadata,
                "data_source_timestamp": data_source_timestamp,
                "generation": provenance,
            },
            execution_policy=execution_policy,
        )
    except SignalFreezeDeadlineExceeded as exc:
        meta["freeze_completed_at"] = exc.observed_at
        return _freeze_rejection(strategy, meta)
    meta["freeze_completed_at"] = str(
        saved.get("freeze_transaction_checked_at") or datetime.now().isoformat(timespec="seconds")
    )
    if close_fallback:
        meta["recommendation_closed_at"] = meta["freeze_completed_at"]
    else:
        meta["recommendation_frozen_at"] = meta["freeze_completed_at"]
    return {"ok": True, "strategy": strategy, "saved": saved, "meta": meta}


def run_snapshots(
    provider,
    validation_store,
    strategies: Iterable[str],
    market: str = "all",
    snapshot_phase: str = PRECLOSE_TRADEABLE,
) -> List[Dict[str, object]]:
    context = _prepare_snapshot_context(provider)
    if not context.get("snapshot_id"):
        context["snapshot_id"] = "snapshot_{}".format(uuid4().hex)
    if not context.get("snapshot_cutoff"):
        context["snapshot_cutoff"] = datetime.now().isoformat(timespec="seconds")
    results: List[Dict[str, object]] = []
    for strategy in strategies:
        try:
            results.append(
                run_snapshot(
                    provider,
                    validation_store,
                    strategy,
                    market=market,
                    _context=context,
                    snapshot_phase=snapshot_phase,
                )
            )
        except Exception as exc:
            results.append(
                {
                    "ok": False,
                    "strategy": strategy,
                    "error": str(exc),
                    "saved": {"saved": 0, "replaced": 0},
                    "meta": {
                        "generated_at": str(context.get("signal_time") or context.get("snapshot_cutoff") or ""),
                        "snapshot_id": str(context.get("snapshot_id") or ""),
                        "rejection": "unhandled_exception",
                    },
                }
            )
    return results


def run_missing_close_snapshots(
    provider,
    validation_store,
    strategies: Iterable[str],
    market: str = "all",
    *,
    now: datetime | None = None,
    _context: Dict[str, object] = None,
) -> Dict[str, object]:
    observed = now or datetime.now()
    close_time = str(getattr(config, "MARKET_CLOSE_TIME", "15:00"))
    signal_date = observed.date().isoformat()
    normalized_strategies = [storage_strategy_name(item) for item in strategies or []]
    normalized_strategies = [item for item in normalized_strategies if item in SNAPSHOT_STRATEGIES]
    if not market_close_reached(observed, close_time):
        return {
            "ok": False,
            "status": "market_not_closed",
            "signal_date": signal_date,
            "snapshots": [],
        }

    existing = {}
    missing = []
    for strategy in normalized_strategies:
        batch = validation_store.saved_signal_batch(strategy, signal_date)
        if batch:
            existing[strategy] = batch
        else:
            missing.append(strategy)
    results = [
        {
            "ok": True,
            "strategy": strategy,
            "status": "already_saved",
            "saved": {"saved": int(batch.get("saved_count") or 0), "replaced": 0},
            "meta": {
                **phase_payload(batch.get("snapshot_phase"), as_of=batch.get("signal_time")),
                "generated_at": str(batch.get("signal_time") or ""),
                "snapshot_id": str(batch.get("snapshot_id") or ""),
            },
        }
        for strategy, batch in existing.items()
    ]
    if missing:
        context = _context or _prepare_snapshot_context(provider)
        context = dict(context or {})
        signal_time = observed.isoformat(timespec="seconds")
        context["snapshot_cutoff"] = signal_time
        context["signal_time"] = signal_time
        context["snapshot_id"] = "close_snapshot_{}".format(uuid4().hex)
        context.pop("pit_archive", None)
        generated = [
            run_snapshot(
                provider,
                validation_store,
                strategy,
                market=market,
                _context=context,
                snapshot_phase=CLOSE_FALLBACK,
            )
            for strategy in missing
        ]
        for item in generated:
            if item.get("ok") and int((item.get("saved") or {}).get("saved") or 0) <= 0:
                item["ok"] = False
                item["error"] = "收盘评分没有产生可保存的推荐股票。"
                item["status"] = "empty_close_recommendations"
        results.extend(generated)
    failed = [item for item in results if not item.get("ok")]
    return {
        "ok": not failed,
        "status": "complete" if not failed else "partial_failure",
        "signal_date": signal_date,
        "existing_strategies": sorted(existing),
        "generated_strategies": [item.get("strategy") for item in results if item.get("status") != "already_saved"],
        "snapshots": results,
    }


def _archive_point_in_time_quotes(
    validation_store,
    context: Dict[str, object],
    provider,
    sample_type: str = SNAPSHOT_SAMPLE_TYPE,
    sample_source: str = SNAPSHOT_SAMPLE_SOURCE,
) -> Dict[str, object]:
    cached = context.get("pit_archive")
    if isinstance(cached, dict) and cached.get("snapshot_id"):
        return cached
    db_path = str(getattr(validation_store, "db_path", "") or "").strip()
    if not db_path:
        return {"status": "unavailable", "reason": "validation_db_path_missing"}
    quotes = context.get("quotes")
    health = _provider_health(provider)
    quote_timestamp = ""
    try:
        quote_timestamp = str((quotes.attrs or {}).get("quote_timestamp") or "")
    except Exception:
        quote_timestamp = ""
    archived = PointInTimeSnapshotStore(db_path).save(
        str(context.get("snapshot_id") or ""),
        quotes,
        captured_at=str(context.get("signal_time") or context.get("snapshot_cutoff") or ""),
        data_source_timestamp=str(
            quote_timestamp
            or health.get("last_quote_refresh")
            or context.get("signal_time")
            or context.get("snapshot_cutoff")
            or ""
        ),
        source=str(health.get("quotes_source") or ""),
        sample_type=sample_type,
        sample_source=sample_source,
    )
    context["pit_archive"] = archived
    return archived


def _snapshot_error_rejection(
    *,
    strategy: str,
    provider,
    validation_store,
    snapshot_id: str,
    signal_time: str,
    candidates,
    quotes,
    event_payload: Dict[str, object],
    fundamental_payload: Dict[str, object],
    error: object,
    market: str,
    market_regime: Dict[str, object],
    generation_status: str = "runtime_exception",
    reason: str = "snapshot_scoring_failed",
    batch_metadata: Optional[Dict[str, object]] = None,
    snapshot_phase: str = PRECLOSE_TRADEABLE,
) -> Dict[str, object]:
    strategy = storage_strategy_name(strategy)
    error_text = str(error)
    generated_at = str(signal_time)
    provider_health = _provider_health(provider)
    quote_timestamp = ""
    if quotes is not None:
        try:
            quote_timestamp = str((quotes.attrs or {}).get("quote_timestamp") or "")
        except Exception:
            quote_timestamp = ""
    data_source_timestamp = str(
        quote_timestamp
        or provider_health.get("last_quote_refresh")
        or ""
    )
    candidate_rows: List[Dict[str, object]] = []
    try:
        candidate_rows = build_candidate_snapshot_rows(
            quotes,
            candidates,
            [],
            generated_at,
            event_payload=event_payload,
            fundamental_payload=fundamental_payload,
            provider_health=provider_health,
            strategy_name=strategy,
            snapshot_id=snapshot_id,
        )
        _attach_frozen_regime(candidate_rows, market_regime)
    except Exception:
        candidate_rows = []
    generation = {
        "status": str(generation_status or "runtime_exception"),
        "strategy": strategy,
        "reason": str(reason or "snapshot_scoring_failed"),
        "error": error_text,
    }
    batch_metadata = dict(batch_metadata or {})
    snapshot_phase = normalize_snapshot_phase(snapshot_phase, PRECLOSE_TRADEABLE)
    batch_metadata.setdefault("snapshot_id", snapshot_id)
    batch_metadata.setdefault("data_source_timestamp", data_source_timestamp)
    batch_metadata.setdefault("market_data_cutoff", generated_at)
    batch_metadata.setdefault("freeze_deadline", _freeze_deadline(generated_at))
    batch_metadata.setdefault("sample_type", SNAPSHOT_SAMPLE_TYPE)
    batch_metadata.setdefault("sample_source", SNAPSHOT_SAMPLE_SOURCE)
    batch_metadata.setdefault("snapshot_phase", snapshot_phase)
    batch_metadata["generation"] = generation
    if snapshot_phase == CLOSE_FALLBACK:
        return {
            "ok": False,
            "strategy": strategy,
            "saved": {"saved": 0, "replaced": 0},
            "meta": {
                **phase_payload(snapshot_phase, as_of=generated_at),
                "generated_at": generated_at,
                "snapshot_id": snapshot_id,
                "rejection": str(generation_status or "runtime_exception"),
                "error": error_text,
            },
        }
    execution_policy = build_execution_policy(strategy, market, snapshot_phase=snapshot_phase)
    version = str(current_strategy_version(strategy) or strategy)
    try:
        saved = validation_store.save_signals(
            strategy,
            version,
            generated_at,
            [],
            candidate_rows=candidate_rows,
            batch_metadata=batch_metadata,
            execution_policy=execution_policy,
        )
    except SignalFreezeDeadlineExceeded as exc:
        # Preserve the actionable source error (for example, a forbidden
        # local quote snapshot) when the diagnostic batch itself misses the
        # publication deadline.  The rejected batch must not mask its cause.
        return {
            "ok": False,
            "strategy": strategy,
            "error": error_text,
            "saved": {"saved": 0, "replaced": 0},
            "meta": {
                "generated_at": generated_at,
                "snapshot_id": snapshot_id,
                "rejection": "freeze_deadline_exceeded_during_error_batch",
                "original_error": error_text,
                "freeze_error": str(exc),
            },
        }
    except Exception as exc:
        return {
            "ok": False,
            "strategy": strategy,
            "error": str(exc),
            "saved": {"saved": 0, "replaced": 0},
            "meta": {
                "generated_at": generated_at,
                "snapshot_id": snapshot_id,
                "rejection": "snapshot_error_save_failed",
                "original_error": error_text,
            },
        }
    return {
        "ok": False,
        "strategy": strategy,
        "saved": saved,
        "meta": {
            "generated_at": generated_at,
            "snapshot_id": snapshot_id,
            "rejection": str(generation_status or "runtime_exception"),
            "error": error_text,
        },
    }


def build_deepseek_precompute_rows(
    provider,
    strategies: Iterable[str],
    market: str = "all",
) -> Dict[str, object]:
    """Build the same local candidate pools used by snapshots, without freezing signals."""
    context = _prepare_snapshot_context(provider)
    if context.get("error"):
        return {"ok": False, "error": context["error"], "rows_by_strategy": {}}
    limit = max(1, int(getattr(config, "DEEPSEEK_FEATURE_REVIEW_LIMIT", 30)))
    rows_by_strategy: Dict[str, List[Dict[str, object]]] = {}
    meta_by_strategy: Dict[str, Dict[str, object]] = {}
    requested_strategies = [storage_strategy_name(item) for item in strategies or []]
    for raw_strategy in requested_strategies:
        strategy = storage_strategy_name(raw_strategy)
        if strategy not in SNAPSHOT_STRATEGIES:
            continue
        rows, meta, version = _score_snapshot_strategy(
            provider,
            context["candidates"].copy(deep=True),
            context["quotes"],
            strategy,
            market,
            dict(context["market_regime"]),
        )
        candidate_pool = meta.get("_candidate_pool_rows") if isinstance(meta, dict) else None
        candidate_pool = candidate_pool if isinstance(candidate_pool, list) and candidate_pool else rows
        _attach_frozen_regime(candidate_pool, context["market_regime"])
        eligible = [
            dict(row)
            for row in candidate_pool or []
            if isinstance(row, dict) and row.get("eligible") is not False
        ]
        rows_by_strategy[strategy] = eligible[:limit]
        meta_by_strategy[strategy] = {
            "strategy_version": version,
            "candidate_count": len(candidate_pool or []),
            "selected_count": len(rows_by_strategy[strategy]),
        }
    if "long_term_watch" in requested_strategies:
        long_term_rows = LongTermWatchScorer().score(
            rows_by_strategy,
            context["candidates"],
            top_n=limit,
        )
        rows_by_strategy["long_term_watch"] = long_term_rows
        meta_by_strategy["long_term_watch"] = {
            "strategy_version": "long_term_watch_v1_deepseek_25pct",
            "candidate_count": len(long_term_rows),
            "selected_count": len(long_term_rows),
        }
    rows_by_strategy, shared_count = _shared_deepseek_candidate_pool(rows_by_strategy)
    for strategy, meta in meta_by_strategy.items():
        meta["selected_count"] = len(rows_by_strategy.get(strategy) or [])
        meta["shared_candidate_count"] = shared_count
    return {
        "ok": True,
        "snapshot_id": str(context["snapshot_id"]),
        "cutoff_at": str(context["snapshot_cutoff"]),
        "rows_by_strategy": rows_by_strategy,
        "meta_by_strategy": meta_by_strategy,
    }


def _shared_deepseek_candidate_pool(rows_by_strategy: Dict[str, List[Dict[str, object]]]):
    limit = max(1, int(getattr(config, "DEEPSEEK_SHARED_RESEARCH_LIMIT", 24)))
    allocations = (
        ("today_term", 35),
        ("tomorrow_picks", 30),
        ("swing_picks", 30),
        ("long_term_watch", 25),
    )
    shared_codes = []
    for strategy, allocation in allocations:
        for row in (rows_by_strategy.get(strategy) or [])[:allocation]:
            code = normalize_code(row.get("code"))
            if code and code not in shared_codes:
                shared_codes.append(code)
            if len(shared_codes) >= limit:
                break
        if len(shared_codes) >= limit:
            break
    if len(shared_codes) < limit:
        for rows in rows_by_strategy.values():
            for row in rows or []:
                code = normalize_code(row.get("code"))
                if code and code not in shared_codes:
                    shared_codes.append(code)
                if len(shared_codes) >= limit:
                    break
            if len(shared_codes) >= limit:
                break
    allowed = set(shared_codes)
    limited = {
        strategy: [dict(row) for row in rows or [] if normalize_code(row.get("code")) in allowed]
        for strategy, rows in rows_by_strategy.items()
    }
    return limited, len(shared_codes)


def _prepare_snapshot_context(provider) -> Dict[str, object]:
    quotes = provider.get_realtime_quotes()
    freshness_error = _quote_freshness_error(provider, quotes)
    if freshness_error:
        return {"error": freshness_error}
    factor_quality_error = _snapshot_factor_quality_error(quotes)
    if factor_quality_error:
        return {"error": factor_quality_error}
    snapshot_cutoff = datetime.now().isoformat(timespec="seconds")
    event_payload = filter_point_in_time_events(load_event_risk(provider), snapshot_cutoff)
    candidates = attach_event_risk(prepare_candidates(quotes), event_payload)
    codes = candidates["code"].tolist() if candidates is not None and "code" in candidates.columns else []
    fundamental_payload = filter_point_in_time_fundamentals(
        load_fundamentals(provider, codes=codes),
        snapshot_cutoff,
    )
    candidates = attach_fundamental_factors(candidates, fundamental_payload)
    candidates = _attach_snapshot_history_factors(provider, candidates)
    return {
        "quotes": quotes,
        "snapshot_cutoff": snapshot_cutoff,
        "snapshot_id": "snapshot_{}".format(uuid4().hex),
        "event_payload": event_payload,
        "fundamental_payload": fundamental_payload,
        "candidates": candidates,
        "market_regime": build_market_regime(candidates, breadth_source=quotes),
    }


def _snapshot_factor_quality_error(quotes) -> str:
    if quotes is None or quotes.empty:
        return "实时行情为空，拒绝生成验证快照。"
    minimum_ratio = min(
        1.0,
        max(0.0, float(getattr(config, "VALIDATION_REQUIRED_FACTOR_COVERAGE_RATIO", 0.99))),
    )
    if "price" in quotes.columns:
        active = quotes[pd.to_numeric(quotes["price"], errors="coerce").fillna(0) > 0]
    else:
        active = quotes
    if active.empty:
        return "实时行情没有有效价格，拒绝生成验证快照。"
    degraded = []
    for field in SNAPSHOT_REQUIRED_FACTOR_FIELDS:
        if field not in active.columns:
            coverage = 0.0
        else:
            coverage = float(pd.to_numeric(active[field], errors="coerce").notna().mean())
        if coverage < minimum_ratio:
            degraded.append("{}={:.1%}".format(field, coverage))
    if not degraded:
        return ""
    return "快照关键评分字段覆盖不足（最低 {:.0%}）：{}；拒绝把缺失值当 0 写入验证样本。".format(
        minimum_ratio,
        ", ".join(degraded),
    )


def _attach_frozen_regime(rows, market_regime: Dict[str, object]) -> None:
    if not isinstance(rows, list):
        return
    regime = dict(market_regime or {})
    level = str(regime.get("level") or "unknown")
    label = str(regime.get("label") or level)
    for row in rows:
        if isinstance(row, dict):
            row["market_regime"] = level
            row["market_regime_label"] = label


def _signal_at_or_after_freeze_cutoff(signal_time: str) -> bool:
    text = str(signal_time or "")
    clock = text.split("T", 1)[1][:5] if "T" in text else ""
    cutoff = str(getattr(config, "RECOMMENDATION_FREEZE_CUTOFF_TIME", "14:50"))[:5]
    return bool(clock and cutoff and clock >= cutoff)


def _freeze_deadline(signal_time: str) -> str:
    signal_date = str(signal_time or "")[:10]
    cutoff = str(getattr(config, "RECOMMENDATION_FREEZE_CUTOFF_TIME", "14:50"))[:5]
    return "{}T{}:00".format(signal_date, cutoff) if signal_date and cutoff else ""


def _freeze_rejection(strategy: str, meta: Dict[str, object]) -> Dict[str, object]:
    return {
        "ok": False,
        "strategy": strategy,
        "error": "{}未能在{}前完成冻结，拒绝保存当日推荐批次。".format(
            strategy,
            getattr(config, "RECOMMENDATION_FREEZE_CUTOFF_TIME", "14:50")
        ),
        "saved": {"saved": 0, "replaced": 0},
        "meta": meta,
    }


def _score_snapshot_strategy(
    provider,
    candidates,
    quotes,
    strategy: str,
    market: str,
    market_regime: Dict[str, object],
    *,
    validation_store=None,
    signal_time: str = "",
):
    scoring_context = None
    if validation_store is not None:
        scoring_context = {
            "_post_score_rows": lambda rows: attach_and_merge_rows(
                rows,
                strategy,
                validation_store,
                signal_time=signal_time,
            )
        }
    if strategy == "today_term":
        rows_by_horizon, meta = score_today_picks(
            candidates,
            hot_ranks={},
            industry_strength={},
            sentiment_lookup={},
            top_n=getattr(config, "RECOMMENDATION_DISPLAY_LIMIT", 18),
            market_filter=market,
            market_regime=market_regime,
            capture_candidate_pool=True,
            scoring_context=scoring_context,
        )
        rows = rows_by_horizon.get("today_term", [])
        return rows, meta, config.TODAY_TERM_STRATEGY_VERSION
    scorers = {
        "tomorrow_picks": (
            score_tomorrow_picks,
            getattr(config, "TOMORROW_SNAPSHOT_TOP_N", config.TOMORROW_TOP_N),
            {"display_cap": 0, "capture_candidate_pool": True, "scoring_context": scoring_context},
        ),
        "swing_picks": (
            score_swing_2_5d_picks,
            getattr(config, "RECOMMENDATION_DISPLAY_LIMIT", 18),
            {"capture_candidate_pool": True, "scoring_context": scoring_context},
        ),
    }
    scorer, top_n, extra_kwargs = scorers[strategy]
    rows, meta = scorer(candidates, top_n=top_n, market_filter=market, market_regime=market_regime, **extra_kwargs)
    return rows, meta, meta.get("strategy_version", strategy)


def _attach_snapshot_history_factors(provider, candidates):
    if candidates is None or candidates.empty or not getattr(config, "ENABLE_HISTORY_FACTORS", True):
        return candidates
    if "code" not in candidates.columns:
        return candidates
    target_codes = candidates.sort_values(["pct_chg", "turnover"], ascending=False).head(
        max(1, int(getattr(config, "HISTORY_FACTOR_LIMIT", 40)))
    )["code"].tolist()
    history_by_code = {}
    try:
        history_by_code.update(
            load_history_frames(getattr(config, "MARKET_DATA_DB_PATH", ""), target_codes, days=90)
        )
    except Exception:
        history_by_code = {}
    for code_value in target_codes:
        code = normalize_code(code_value)
        if not code or code in history_by_code:
            continue
        history = None
        try:
            if hasattr(provider, "get_cached_history"):
                history = provider.get_cached_history(code, days=90)
            elif hasattr(provider, "get_history"):
                history = provider.get_history(code, days=90)
        except Exception:
            history = None
        if history is not None and not history.empty:
            history_by_code[code] = history
    if not history_by_code:
        return candidates
    factors = build_alphalite_factors(history_by_code)
    if factors is not None and not factors.empty:
        cutoffs = {}
        for code, history in history_by_code.items():
            if history is None or history.empty or "trade_date" not in history.columns:
                continue
            cutoffs[normalize_code(code)] = str(history["trade_date"].iloc[-1])
        factors["history_data_cutoff"] = factors["code"].map(cutoffs).fillna("")
    return merge_alphalite(candidates, factors)


def _quote_freshness_error(provider, quotes) -> str:
    if quotes is None or quotes.empty:
        return "行情为空，拒绝保存推荐快照。"
    min_rows = int(getattr(config, "QUOTE_SNAPSHOT_MIN_ROWS", 50))
    if len(quotes) < min_rows:
        return "行情行数不足 {} 条，拒绝保存推荐快照。".format(min_rows)
    health_fn = getattr(provider, "health", None)
    if not callable(health_fn):
        return ""
    health = health_fn() or {}
    source = str(health.get("quotes_source") or "")
    if not source or source == "unavailable":
        return "行情来源不可用，拒绝保存推荐快照。"
    if "快照" in source and not getattr(config, "VALIDATION_ALLOW_LOCAL_QUOTE_SNAPSHOT", False):
        return "当前行情来自本地快照，拒绝保存为今日真实推荐。"
    refreshed = health.get("last_quote_refresh")
    if not refreshed:
        return "缺少行情刷新时间，拒绝保存推荐快照。"
    try:
        refreshed_at = datetime.fromisoformat(str(refreshed))
    except ValueError:
        return "行情刷新时间格式异常，拒绝保存推荐快照。"
    max_age = int(getattr(config, "VALIDATION_SNAPSHOT_MAX_QUOTE_AGE_SECONDS", 900))
    age = (datetime.now() - refreshed_at).total_seconds()
    if age > max_age:
        return "行情已超过 {} 秒未刷新，拒绝保存推荐快照。".format(max_age)
    return ""


def _provider_health(provider) -> Dict[str, object]:
    health_fn = getattr(provider, "health", None)
    if not callable(health_fn):
        return {}
    try:
        result = health_fn() or {}
    except Exception:
        return {}
    return result if isinstance(result, dict) else {}
