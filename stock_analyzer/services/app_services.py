from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Dict, List, Tuple

import pandas as pd

from .. import config
from ..background_workers import BackgroundWorkerGroup
from ..recommendation_freeze import recommendation_is_frozen
from ..app_response_support import (
    error_payload,
    response_payload,
    saved_swing_fallback_payload,
    saved_tomorrow_fallback_payload,
    snapshot_fallback_payload,
)
from ..app_runtime_support import risk_blacklist_summary
from ..app_support import (
    apply_strategy_validation_gate,
    attach_validation_summary,
    candidate_code_rows,
    demote_strategy_rows_to_backup,
    load_local_history_frames,
    market_news as fetch_market_news,
    quote_lookup,
    sentiment_for_candidates,
    stock_exists_in_quotes,
    strategy_validation_gate_decision,
    validation_batch_summary,
    validation_gate_window_days,
)
from ..backtest import parse_code_list
from ..deepseek_scheduler import deepseek_schedule_status
from ..event_risk import load_event_risk
from ..factor_ic import load_factor_ic
from ..fundamentals import load_fundamentals
from ..normalization import coerce_number, normalize_code
from ..oos_report import generate_strategy_oos_report
from ..recommendation_runtime_support import (
    apply_deepseek_to_reviewable_rows,
    build_recommendation_horizons,
    finalize_recommendation_payload_meta,
    scored_strategy_rows,
)
from ..recommendation_runtime_support import prediction_strategy_rows as build_prediction_strategy_rows
from ..recommendation_snapshot import load_recommendation_snapshot
from ..risk_blacklist import load_risk_blacklist
from ..selfcheck import factor_coverage
from ..sentiment import build_market_sentiment_index
from ..snapshot import SNAPSHOT_STRATEGIES, run_missing_close_snapshots, run_snapshot, run_snapshots
from ..snapshot_phase import market_close_reached, normalize_snapshot_phase, phase_payload
from ..strategies import storage_strategy_name
from ..validation_policy import validation_baseline_config
from ..validation_replay import backfill_strategy_validation_samples
from ..validation_audit_cli import build_validation_readiness_report
from ..validation_runtime_support import (
    analysis_window,
    auto_snapshot_time_parts,
    configured_auto_snapshot_strategies,
    next_auto_snapshot_at,
    next_auto_update_window_start,
    run_validation_auto_snapshot_once as run_validation_auto_snapshot_once_support,
    run_validation_auto_update_once as run_validation_auto_update_once_support,
    run_validation_tuning_once as run_validation_tuning_once_support,
    set_status,
    start_validation_auto_snapshot_worker,
    start_validation_auto_update_worker,
    within_auto_update_window,
)
from .recommendation_cache import RecommendationRefreshService, RecommendationSnapshotService


DEFAULT_AUTO_SNAPSHOT_STRATEGIES = tuple(config.AUTO_SNAPSHOT_STRATEGIES)
_VALIDATION_AUTO_WORKERS = set()
_VALIDATION_AUTO_WORKERS_LOCK = threading.Lock()
_LOGGER = logging.getLogger(__name__)


@dataclass
class AppServiceHooks:
    """Patchable functions re-exported from app.py for existing tests."""

    list_market_data_codes: Callable
    load_local_history_frames: Callable
    run_alphalite_backtest: Callable
    run_rolling_alphalite_backtest: Callable


class _AppServiceContext:
    """Shared collaborators and helper operations used by application use cases."""

    def __init__(
        self,
        container,
        hooks: AppServiceHooks,
        schedule_snapshot_save: Callable[[Dict[str, object]], None] | None = None,
    ) -> None:
        self.container = container
        self.hooks = hooks
        self._schedule_snapshot_save_callback = schedule_snapshot_save
        self.provider = container.provider
        self.validation_store = container.validation_store
        self.recommendation_snapshots = RecommendationSnapshotService(
            container.recommendation_cache,
            container.horizon_cache,
            is_frozen=self.recommendation_is_frozen,
            overlay_live_quotes=self._overlay_live_quotes_on_payload,
            snapshot_path=config.RECOMMENDATION_SNAPSHOT_PATH,
            snapshot_max_age_seconds=getattr(config, "RECOMMENDATION_SNAPSHOT_MAX_AGE_SECONDS", 300),
        )
        self.recommendation_refresh = RecommendationRefreshService(
            self.recommendation_snapshots,
            refresh_quotes=container.candidate_pipeline.refresh_quotes,
            build_recommendations=self._build_recommendations_payload,
            build_horizon=self._build_horizon_payload,
            provider_health=self._safe_provider_health,
            research_disclaimer=self.research_disclaimer,
            is_frozen=self.recommendation_is_frozen,
        )
        self.auto_update_lock = threading.Lock()
        self.auto_update_status = {
            "enabled": bool(config.VALIDATION_AUTO_UPDATE_ENABLED),
            "started": False,
            "running": False,
            "last_started_at": "",
            "last_finished_at": "",
            "last_error": "",
            "last_result": {},
            "next_run_after_seconds": config.VALIDATION_AUTO_UPDATE_INITIAL_DELAY_SECONDS,
            "next_run_at": "",
        }
        self.auto_snapshot_lock = threading.Lock()
        self.auto_snapshot_status = {
            "enabled": bool(config.VALIDATION_AUTO_SNAPSHOT_ENABLED),
            "started": False,
            "running": False,
            "schedule_time": config.VALIDATION_AUTO_SNAPSHOT_TIME,
            "market": config.VALIDATION_AUTO_SNAPSHOT_MARKET,
            "last_attempt_date": "",
            "retry_count": 0,
            "deadline_missed": False,
            "deadline_missed_at": "",
            "last_started_at": "",
            "last_finished_at": "",
            "last_error": "",
            "last_result": {},
            "last_tuning_date": "",
            "last_tuning_result": {},
            "next_run_at": "",
        }
        self._validation_workers = BackgroundWorkerGroup(
            (self._start_auto_update_worker, self._start_auto_snapshot_worker)
        )

    def _start_auto_update_worker(self, stop_event: threading.Event) -> threading.Thread | None:
        return start_validation_auto_update_worker(
            worker_set=_VALIDATION_AUTO_WORKERS,
            worker_lock=_VALIDATION_AUTO_WORKERS_LOCK,
            set_auto_update_status=self._set_auto_update_status,
            within_auto_update_window_fn=lambda now: within_auto_update_window(
                now,
                getattr(config, "VALIDATION_AUTO_UPDATE_START_TIME", "14:30"),
                getattr(config, "VALIDATION_AUTO_UPDATE_UNTIL_TIME", "23:59"),
            ),
            next_auto_update_window_start_fn=lambda now: next_auto_update_window_start(
                now,
                getattr(config, "VALIDATION_AUTO_UPDATE_START_TIME", "14:30"),
                getattr(config, "VALIDATION_AUTO_UPDATE_UNTIL_TIME", "23:59"),
            ),
            run_validation_auto_update_once_fn=self.run_validation_auto_update_once,
            stop_event=stop_event,
        )

    def _start_auto_snapshot_worker(self, stop_event: threading.Event) -> threading.Thread | None:
        return start_validation_auto_snapshot_worker(
            worker_set=_VALIDATION_AUTO_WORKERS,
            worker_lock=_VALIDATION_AUTO_WORKERS_LOCK,
            auto_snapshot_lock=self.auto_snapshot_lock,
            auto_snapshot_status=self.auto_snapshot_status,
            auto_snapshot_time_parts_fn=lambda: auto_snapshot_time_parts(config.VALIDATION_AUTO_SNAPSHOT_TIME),
            next_auto_snapshot_at_fn=lambda now: next_auto_snapshot_at(now, config.VALIDATION_AUTO_SNAPSHOT_TIME),
            set_auto_snapshot_status=self._set_auto_snapshot_status,
            run_validation_auto_snapshot_once_fn=self.run_validation_auto_snapshot_once,
            stop_event=stop_event,
        )

    def start_background_workers(self) -> bool:
        return self._validation_workers.start()

    def stop_background_workers(self, timeout_seconds: float = 5.0) -> None:
        self._validation_workers.stop(timeout_seconds)

    def index_context(self) -> Dict[str, object]:
        return {
            "refresh_seconds": config.REFRESH_SECONDS,
            "default_top_n": config.DEFAULT_TOP_N,
            "recommendation_snapshot_max_age_seconds": getattr(
                config,
                "RECOMMENDATION_SNAPSHOT_MAX_AGE_SECONDS",
                300,
            ),
        }

    def empty_stream_status(self) -> Tuple[Dict[str, str], int]:
        return {"Cache-Control": "no-cache"}, 204

    def health_payload(self) -> Tuple[Dict[str, object], int]:
        coverage = {
            "row_count": 0,
            "avg_data_coverage": 0.0,
            "alphalite_ready_ratio": 0.0,
            "alphalite_not_ready_ratio": 1.0,
            "alphalite_zero_coverage_ratio": 1.0,
            "columns": {},
            "degraded": True,
            "alerts": [],
        }
        blacklist_payload = load_risk_blacklist()
        try:
            quotes = self._current_quotes()
            candidates, _ = self._candidates_with_regime_from_quotes(quotes)
            coverage = factor_coverage(candidates)
        except Exception:
            pass
        provider_health = self._provider_health_with_factor_coverage(coverage=coverage)
        try:
            provider_health["cache"] = self.container.cache_health()
            provider_health["snapshot_writer"] = self.container.snapshot_writer_health()
            provider_health["recommendation_refresh_workers"] = self.recommendation_refresh.status()
        except Exception:
            pass
        health_metrics = self._validation_health_metrics()
        return {
            "ok": True,
            "refresh_seconds": config.REFRESH_SECONDS,
            "supported_markets": config.MARKET_LABELS,
            "factor_coverage": coverage,
            "runtime_metrics": health_metrics,
            "event_risk": {
                "enabled": bool(config.ENABLE_EVENT_RISK),
                "status": load_event_risk(self.provider).get("status", "disabled"),
            },
            "risk_blacklist": risk_blacklist_summary(blacklist_payload),
            "factor_ic": {
                "enabled": bool(config.ENABLE_FUNDAMENTALS),
                "fundamentals_status": load_fundamentals(self.provider).get("status", "disabled"),
                "generated_at": load_factor_ic().get("generated_at", ""),
            },
            "deepseek_schedule": deepseek_schedule_status(),
            "health": provider_health,
        }, 200

    def _validation_health_metrics(self) -> Dict[str, object]:
        metrics: Dict[str, object] = {
            "unfilled_rate": 0.0,
            "open_positions": 0,
            "db_lock_wait_ms": 0.0,
            "recent_snapshot_at": "",
            "snapshot_writer_success": False,
            "snapshot_writer_failure_count": 0,
            "candidate_coverage": 0.0,
        }
        try:
            status_counts = self.validation_store.repository.signal_status_counts(
                days=120,
            )
            signal_sample_count = int(status_counts.get("signal_sample_count") or 0)
            if signal_sample_count > 0:
                unfilled = int(status_counts.get("unfilled_outcome_count") or 0)
                metrics["unfilled_rate"] = round(unfilled * 100.0 / signal_sample_count, 4)
                outcome_coverage = float(status_counts.get("outcome_coverage_pct") or 0.0)
                metrics["candidate_coverage"] = float(outcome_coverage)
        except Exception:
            pass
        try:
            with self.validation_store.repository.connect() as conn:
                row = conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM strategy_execution_records
                    WHERE COALESCE(position_status, '') = 'open_position'
                    """,
                ).fetchone()
            open_positions = int(row[0] or 0) if row else 0
            metrics["open_positions"] = open_positions
        except Exception:
            pass
        try:
            writer = self.container.snapshot_writer_health()
            metrics["recent_snapshot_at"] = str(writer.get("last_success_at") or "")
            metrics["snapshot_writer_failure_count"] = int(writer.get("failure_count") or 0)
            metrics["snapshot_writer_success"] = bool(int(writer.get("success_count") or 0) > 0)
        except Exception:
            pass
        return metrics

    def recommendations_payload(self, top_n: int, market: str) -> Tuple[Dict[str, object], int]:
        if market_close_reached(
            datetime.now(),
            str(getattr(config, "MARKET_CLOSE_TIME", "15:00")),
        ):
            after_close = self._after_close_recommendations_payload(top_n, market)
            if after_close is not None:
                return self._overlay_live_quotes_on_payload(after_close), 200
        refresh_after_seconds = max(5, int(config.REFRESH_SECONDS))
        entry = self._cached_recommendation_entry(top_n, market) or self._snapshot_entry(top_n, market)
        if entry is not None:
            snapshot = self._recommendation_snapshot_info(entry)
            age_seconds = float(snapshot.get("age_seconds") or 0.0)
            if age_seconds >= refresh_after_seconds or snapshot.get("stage") != "ready":
                self._schedule_recommendation_refresh(top_n, market)
            return self._serve_recommendation_payload(entry), 200
        payload, status = self._build_recommendations_payload(top_n, market, include_deepseek=False)
        if status == 200:
            self._schedule_recommendation_refresh(top_n, market)
            entry = self._cached_recommendation_entry(top_n, market)
            if entry is not None:
                return self._serve_recommendation_payload(entry), status
            return payload, status
        refresh_status = getattr(self.provider, "quote_refresh_status", lambda: {})()
        if refresh_status.get("running") or self._safe_provider_health().get("quotes_source") == "后台刷新中":
            return response_payload(
                self._safe_provider_health,
                self.research_disclaimer,
                ok=True,
                include_disclaimer=True,
                data=[],
                recommendations={"today_term": [], "tomorrow_picks": [], "swing_picks": []},
                meta={
                    "generated_at": "",
                    "candidate_count": 0,
                    "display_count": 0,
                    "display_limit": top_n,
                    "top_n": top_n,
                    "market_filter": market,
                    "strategy": "后台刷新中",
                    "fallback": "async_refresh_pending",
                },
                snapshot={"saved_at": "", "age_seconds": None, "source": "async_refresh_pending"},
            ), 200
        return payload, status

    def latest_recommendations_payload(
        self,
        top_n: int,
        market: str,
        max_age: int,
    ) -> Tuple[Dict[str, object], int]:
        if market_close_reached(
            datetime.now(),
            str(getattr(config, "MARKET_CLOSE_TIME", "15:00")),
        ):
            after_close = self._after_close_recommendations_payload(top_n, market)
            if after_close is not None:
                return self._overlay_live_quotes_on_payload(after_close), 200
        snapshot = load_recommendation_snapshot(
            config.RECOMMENDATION_SNAPSHOT_PATH,
            max_age_seconds=max_age,
            expected_market=market,
            expected_top_n=top_n,
        )
        if snapshot.get("ok"):
            payload = self._overlay_live_quotes_on_payload(dict(snapshot["payload"]))
            payload["snapshot"] = {
                "saved_at": snapshot.get("saved_at", ""),
                "age_seconds": snapshot.get("age_seconds"),
                "path": snapshot.get("path"),
            }
            return payload, 200
        return self.json_payload(ok=False, status=404, snapshot=snapshot)

    def horizon_payload(self, strategy: str, top_n: int, market: str) -> Tuple[Dict[str, object], int]:
        try:
            if market_close_reached(
                datetime.now(),
                str(getattr(config, "MARKET_CLOSE_TIME", "15:00")),
            ):
                signal_date = datetime.now().date().isoformat()
                run_missing_close_snapshots(
                    self.provider,
                    self.validation_store,
                    [strategy],
                    market=market,
                )
                saved = self._saved_horizon_payload(
                    strategy,
                    top_n,
                    market,
                    signal_date=signal_date,
                )
                if saved is not None:
                    return self._overlay_live_quotes_on_payload(saved), 200
            payload, status = self._horizon_payload(strategy, top_n, market)
            return self._overlay_live_quotes_on_payload(payload), status
        except Exception as exc:
            if strategy == "tomorrow_picks":
                saved_rows = self.validation_store.latest_signal_rows("tomorrow_picks")
                if saved_rows:
                    payload = saved_tomorrow_fallback_payload(
                        saved_rows=saved_rows,
                        top_n=top_n,
                        market=market,
                        detailed=True,
                        validation_store=self.validation_store,
                        cached_metrics_fn=self.cached_metrics,
                        load_risk_blacklist_fn=load_risk_blacklist,
                        analysis_window_fn=lambda: analysis_window(config.VALIDATION_AUTO_SNAPSHOT_TIME),
                        provider_health_fn=self.provider.health,
                        research_disclaimer_fn=self.research_disclaimer,
                    )
                    return self._overlay_live_quotes_on_payload(payload), 200
            try:
                payload, status = self._horizon_payload(strategy, top_n, market)
                return self._overlay_live_quotes_on_payload(payload), status
            except Exception:
                pass
            return self.error_payload(exc, include_disclaimer=True)

    def stock_prediction_payload(self, code: str) -> Tuple[Dict[str, object], int]:
        normalized_code = code.strip()[:12]
        try:
            quotes, quote_error = self._current_quotes_or_empty()
            candidates, market_regime = self._candidates_with_regime_from_quotes(
                quotes,
                attach_codes=[normalized_code],
            )
            top_n = max(1, len(candidates))
            prediction_sentiment_lookup = {}
            if (
                candidates is not None
                and not candidates.empty
                and "pct_chg" in candidates.columns
                and "code" in candidates.columns
                and "name" in candidates.columns
            ):
                candidate_subset = candidates.sort_values("pct_chg", ascending=False).head(80)
                prediction_sentiment_lookup = sentiment_for_candidates(
                    self.provider,
                    self.container.sentiment_cache,
                    candidate_subset[["code", "name"]].to_dict("records"),
                )
            short_term_snapshot_rows = None
            short_term_snapshot_meta = None
            snapshot = load_recommendation_snapshot(
                config.RECOMMENDATION_SNAPSHOT_PATH,
                max_age_seconds=getattr(config, "RECOMMENDATION_SNAPSHOT_MAX_AGE_SECONDS", 300),
                expected_market="all",
                expected_top_n=config.DEFAULT_TOP_N,
            )
            if snapshot.get("ok"):
                snapshot_payload = snapshot.get("payload") or {}
                short_term_snapshot_rows = (
                    (snapshot_payload.get("recommendations") or {}).get("today_term")
                    or snapshot_payload.get("data")
                    or []
                )
                short_term_snapshot_meta = {
                    "strategy_version": str((snapshot_payload.get("meta") or {}).get("strategy_version", "")),
                    "missed_reason": "未进入当前推荐展示榜，可能因排序靠后、主题限流或稳定性裁剪",
                }
            prediction_rows, strategy_metas = self._prediction_strategy_rows(
                candidates,
                top_n=top_n,
                market_regime=market_regime,
                hot_ranks=self._cached_hot_ranks(),
                industry_strength=self._cached_industry_strength(),
                sentiment_lookup=prediction_sentiment_lookup,
                short_term_rows_override=short_term_snapshot_rows,
                short_term_meta_override=short_term_snapshot_meta,
                cached_metrics_fn=self.cached_metrics,
                validation_store=self.validation_store,
            )
            fallback_history = None
            fallback_error = ""
            normalized_for_lookup = normalize_code(normalized_code)
            if not stock_exists_in_quotes(normalized_for_lookup, quotes):
                try:
                    fallback_history = self.provider.get_history(normalized_for_lookup, days=120)
                except Exception as exc:
                    fallback_error = str(exc)
            if quote_error:
                fallback_error = "; ".join(item for item in (quote_error, fallback_error) if item)
            from ..prediction import build_stock_prediction

            result = build_stock_prediction(
                normalized_code,
                candidates,
                prediction_rows,
                strategy_metas=strategy_metas,
                market_regime=market_regime,
                raw_quotes=quotes,
                fallback_history=fallback_history,
                fallback_error=fallback_error,
            )
            if bool(getattr(config, "ENABLE_STANCE_TRACKING", False)) and bool(result.get("ok")):
                try:
                    result["stance_tracking"] = self.validation_store.save_stock_prediction_snapshot(result)
                except Exception as exc:
                    result["stance_tracking"] = {"saved": 0, "status": "error", "error": str(exc)}
            result_ok = bool(result.get("ok"))
            result_payload = dict(result)
            result_payload.pop("ok", None)
            return self.json_payload(ok=result_ok, **result_payload)
        except Exception as exc:
            return self.error_payload(exc)

    def stock_prediction_stance_validation(self, days: int) -> Tuple[Dict[str, object], int]:
        try:
            return self.json_payload(
                ok=True,
                enabled=bool(getattr(config, "ENABLE_STANCE_TRACKING", False)),
                metrics=self.validation_store.stance_metrics(days=days),
            )
        except Exception as exc:
            return self.error_payload(exc)

    def stock_prediction_stance_validation_update(self, days: int) -> Tuple[Dict[str, object], int]:
        try:
            result = self.validation_store.update_stock_prediction_outcomes(self.provider, days=days)
            return self.json_payload(
                ok=True,
                enabled=bool(getattr(config, "ENABLE_STANCE_TRACKING", False)),
                result=result,
            )
        except Exception as exc:
            return self.error_payload(exc)

    def strategy_snapshot(self, strategy: str, market: str) -> Tuple[Dict[str, object], int]:
        strategy = strategy if strategy in SNAPSHOT_STRATEGIES else "today_term"
        try:
            result = run_snapshot(self.provider, self.validation_store, strategy, market=market)
            self.invalidate_metrics_cache()
            result_ok = bool(result.get("ok"))
            result_payload = dict(result)
            result_payload.pop("ok", None)
            return self.json_payload(ok=result_ok, **result_payload)
        except Exception as exc:
            return self.error_payload(exc)

    def strategy_validation_update(self, signal_date: str, strategy: str) -> Tuple[Dict[str, object], int]:
        try:
            result = self.validation_store.update_outcomes(
                self.provider,
                signal_date=signal_date,
                strategy_name=strategy,
            )
            self.invalidate_metrics_cache()
            return self.json_payload(ok=True, result=result)
        except Exception as exc:
            return self.error_payload(exc)

    def strategy_validation_auto_update_status(self) -> Tuple[Dict[str, object], int]:
        with self.auto_update_lock:
            status = dict(self.auto_update_status)
        with self.auto_snapshot_lock:
            snapshot_status = dict(self.auto_snapshot_status)
        status["config"] = {
            "mode": "recommendation_snapshot",
            "initial_delay_seconds": config.VALIDATION_AUTO_UPDATE_INITIAL_DELAY_SECONDS,
            "interval_seconds": config.VALIDATION_AUTO_UPDATE_INTERVAL_SECONDS,
            "strategies": self._configured_auto_snapshot_strategies(),
            "start_time": getattr(config, "VALIDATION_AUTO_UPDATE_START_TIME", "14:30"),
            "until_time": getattr(config, "VALIDATION_AUTO_UPDATE_UNTIL_TIME", "23:59"),
        }
        snapshot_status["config"] = {
            "enabled": bool(config.VALIDATION_AUTO_SNAPSHOT_ENABLED),
            "time": config.VALIDATION_AUTO_SNAPSHOT_TIME,
            "retry_seconds": getattr(config, "VALIDATION_AUTO_SNAPSHOT_RETRY_SECONDS", 60),
            "market": config.VALIDATION_AUTO_SNAPSHOT_MARKET,
            "strategies": self._configured_auto_snapshot_strategies(),
            "weekdays_only": True,
        }
        return self.json_payload(ok=True, auto_update=status, auto_snapshot=snapshot_status)

    def strategy_validation_prefetch_history(
        self,
        *,
        signal_date: str,
        strategy: str,
        days: int,
        limit: int,
        force: bool,
        update: bool,
    ) -> Tuple[Dict[str, object], int]:
        try:
            code_rows = self.validation_store.signal_codes(
                signal_date=signal_date,
                strategy_name=strategy,
                limit=limit,
            )
            codes = [row["code"] for row in code_rows]
            prefetch = self.provider.prefetch_history(codes, days=days, force=force)
            if int(prefetch.get("downloaded") or 0) > 0:
                self.container.factors_cache.clear()
            outcome = None
            if update:
                outcome = self.validation_store.update_outcomes(
                    self.provider,
                    signal_date=signal_date,
                    strategy_name=strategy,
                )
                self.invalidate_metrics_cache()
            return self.json_payload(ok=True, codes=code_rows, prefetch=prefetch, outcome=outcome)
        except Exception as exc:
            return self.error_payload(exc)

    def strategy_validation_backfill_current_baseline(
        self,
        *,
        strategy: str,
        days: int,
        history_days: int,
        limit: int,
        force: bool,
        execute: bool,
    ) -> Tuple[Dict[str, object], int]:
        try:
            before = self.validation_store.validation_baseline_status(strategy, days=days)
            candidates = self.validation_store.validation_baseline_backfill_candidates(
                strategy,
                days=days,
                limit=limit,
            )
            code_rows = list(candidates.get("codes") or [])
            codes = [row["code"] for row in code_rows if row.get("code")]
            prefetch = None
            outcome = None
            after = before
            if execute and codes:
                prefetch = self.provider.prefetch_history(codes, days=history_days, force=force)
                if int(prefetch.get("downloaded") or 0) > 0:
                    self.container.factors_cache.clear()
                outcome = self.validation_store.update_outcomes(
                    self.provider,
                    strategy_name=strategy,
                    only_incomplete=True,
                )
                self.invalidate_metrics_cache()
                after = self.validation_store.validation_baseline_status(strategy, days=days)
            return self.json_payload(
                ok=True,
                strategy=strategy,
                execute=execute,
                before=before,
                candidates=candidates,
                codes=code_rows,
                prefetch=prefetch,
                outcome=outcome,
                after=after,
            )
        except Exception as exc:
            return self.error_payload(exc)

    def strategy_validation_oos_report(self, strategy: str, days: int) -> Tuple[Dict[str, object], int]:
        try:
            report = generate_strategy_oos_report(
                self.validation_store,
                strategy,
                days,
                strategy_validation_gate_decision,
            )
            return self.json_payload(ok=True, include_health=False, **report)
        except Exception as exc:
            return self.error_payload(exc)

    def strategy_validation_oos_report_history(self, strategy: str, limit: int) -> Tuple[Dict[str, object], int]:
        try:
            reports = self.validation_store.list_oos_reports(strategy_name=strategy, limit=limit)
            return self.json_payload(
                ok=True,
                include_health=False,
                strategy=strategy,
                limit=limit,
                reports=reports,
            )
        except Exception as exc:
            return self.error_payload(exc)

    def strategy_validation_readiness(self) -> Tuple[Dict[str, object], int]:
        try:
            report = build_validation_readiness_report(config.VALIDATION_DB_PATH)
            ready = bool(report.pop("ok", False))
            return self.json_payload(ok=True, include_health=False, ready=ready, **report)
        except Exception as exc:
            return self.error_payload(exc)

    def strategy_validation_portfolio_baseline(
        self,
        *,
        strategy: str,
        days: int,
        signal_date: str,
        ranking_field: str,
        model_id: str,
        execute: bool,
        include_audit: bool,
    ) -> Tuple[Dict[str, object], int]:
        try:
            from ..portfolio_baseline import DailyPortfolioBaselineService

            service = DailyPortfolioBaselineService(self.validation_store)
            if execute:
                result = service.run(
                    self.provider,
                    strategy,
                    signal_date=signal_date,
                    days=days,
                    ranking_field=ranking_field,
                    model_id=model_id,
                )
                if include_audit and signal_date:
                    result["audit_record"] = service.record(
                        strategy,
                        signal_date,
                        ranking_field=ranking_field,
                        model_id=model_id,
                        include_audit=True,
                    )
            elif signal_date:
                result = service.record(
                    strategy,
                    signal_date,
                    ranking_field=ranking_field,
                    model_id=model_id,
                    include_audit=include_audit,
                )
            else:
                result = service.report(
                    strategy,
                    days=days,
                    ranking_field=ranking_field,
                    model_id=model_id,
                    include_audit=include_audit,
                )
            return self.json_payload(ok=True, include_health=False, strategy=strategy, result=result)
        except Exception as exc:
            return self.error_payload(exc)

    def strategy_validation_backfill_samples(
        self,
        *,
        strategy: str,
        days: int,
        replay_days: int,
        top_n: int,
        holding_days: int,
        limit: int,
        force: bool,
    ) -> Tuple[Dict[str, object], int]:
        try:
            code_rows = self.validation_store.signal_codes(strategy_name=strategy, limit=limit)
            if not code_rows:
                code_rows = candidate_code_rows(self.provider, self.container.quotes_cache, limit)
            codes = [row["code"] for row in code_rows]
            code_names = {row["code"]: row.get("name") or row["code"] for row in code_rows}
            prefetch = self.provider.prefetch_history(codes, days=days, force=force)
            if int(prefetch.get("downloaded") or 0) > 0:
                self.container.factors_cache.clear()
            replay = backfill_strategy_validation_samples(
                self.provider,
                self.validation_store,
                strategy,
                codes,
                code_names=code_names,
                days=days,
                replay_days=replay_days,
                top_n=top_n,
                holding_days=holding_days,
            )
            self.invalidate_metrics_cache()
            metrics = self.validation_store.metrics(strategy, days=120)
            return self.json_payload(
                ok=bool(replay.get("ok")),
                codes=code_rows,
                prefetch=prefetch,
                replay=replay,
                metrics=metrics,
            )
        except Exception as exc:
            return self.error_payload(exc)

    def strategy_validation(self, *, strategy: str, days: int, light: bool) -> Tuple[Dict[str, object], int]:
        try:
            dates = self.validation_store.list_signal_dates(strategy)
            if light:
                return self.json_payload(ok=True, include_health=False, strategy=strategy, dates=dates)
            summary = self.cached_strategy_validation_summary(strategy, days)
            return self.json_payload(ok=True, strategy=strategy, dates=dates, **summary)
        except Exception as exc:
            return self.error_payload(exc)

    def strategy_validation_runtime_config(self, strategy: str, days: int) -> Tuple[Dict[str, object], int]:
        baseline = validation_baseline_config(strategy)
        baseline_status = self.validation_store.validation_baseline_status(strategy, days=days)
        return self.json_payload(
            ok=True,
            include_health=False,
            strategy=strategy,
            validation_baseline=baseline,
            validation_baseline_id=baseline.get("baseline_id"),
            baseline_status=baseline_status,
        )

    def strategy_validation_tuning(
        self,
        *,
        strategy: str,
        days: int,
        method: str,
    ) -> Tuple[Dict[str, object], int]:
        try:
            if method == "GET":
                return self.json_payload(
                    ok=True,
                    include_health=False,
                    strategy=strategy,
                    operation="shadow_tuning_suggestion",
                    latest=self.validation_store.latest_tuning_run(strategy),
                )
            tuning_result = self.run_validation_tuning_once([strategy], days=days)
            self.invalidate_metrics_cache()
            run = (tuning_result.get("runs") or [{}])[0]
            latest = run.get("latest") or self.validation_store.latest_tuning_run(strategy)
            plan = latest.get("plan") or {}
            return self.json_payload(
                ok=bool(tuning_result.get("ok")),
                include_health=False,
                strategy=strategy,
                operation="shadow_tuning_suggestion",
                plan=plan,
                input_fingerprint=str(run.get("input_fingerprint") or plan.get("input_fingerprint") or ""),
                reused=bool(run.get("reused")),
                saved=run.get("saved", {}),
                latest=latest,
                tuning=tuning_result,
            )
        except Exception as exc:
            return self.error_payload(exc)

    def strategy_validation_daily(
        self,
        *,
        signal_date: str,
        strategy: str,
        should_update: bool,
        include_quotes: bool,
    ) -> Tuple[Dict[str, object], int]:
        if not signal_date:
            return self.bad_request_payload("缺少 date 参数")
        try:
            batch = self._validation_batch_row(signal_date, strategy)
            rows = self.validation_store.signals_for_date(signal_date, strategy)
            rows, update_result = self._refresh_validation_rows_if_needed(
                rows,
                signal_date=signal_date,
                strategy_name=strategy,
                should_update=should_update,
            )
            self._attach_validation_daily_quotes(rows, include_quotes)
            summary = validation_batch_summary(rows, strategy, batch=batch)
            return self.json_payload(
                ok=True,
                date=signal_date,
                data=rows,
                summary=summary,
                batch=batch or {},
                update=update_result,
            )
        except Exception as exc:
            return self.error_payload(exc)

    def tomorrow_iteration(self, *, days: int, force: bool, direction_focus) -> Tuple[Dict[str, object], int]:
        try:
            cached = self.container.tomorrow_iteration.load()
            if cached and int(cached.get("days") or 0) == days and not force:
                return self.json_payload(ok=True, iteration=cached)
            from ..calibrate import calibrate_live_weights

            result = calibrate_live_weights(
                "tomorrow_picks",
                db_path=config.VALIDATION_DB_PATH,
                top_k=10,
                days=days,
                steps=2,
                dry_run=True,
                direction_focus=direction_focus,
            )
            payload = self.container.tomorrow_iteration.payload(result, days=days)
            self.container.tomorrow_iteration.save(payload)
            return self.json_payload(ok=True, iteration=payload)
        except Exception as exc:
            return self.error_payload(exc, iteration=self.container.tomorrow_iteration.load())

    def tomorrow_iteration_apply(self, *, days: int, direction_focus) -> Tuple[Dict[str, object], int]:
        try:
            from ..calibrate import calibrate_live_weights

            dry_result = calibrate_live_weights(
                "tomorrow_picks",
                db_path=config.VALIDATION_DB_PATH,
                top_k=10,
                days=days,
                steps=2,
                dry_run=True,
                direction_focus=direction_focus,
            )
            dry_payload = self.container.tomorrow_iteration.payload(dry_result, days=days)
            if not dry_payload["can_apply"]:
                self.container.tomorrow_iteration.save(dry_payload)
                return self.json_payload(
                    ok=False,
                    status=409,
                    error=dry_payload["reason"],
                    iteration=dry_payload,
                )
            written_result = calibrate_live_weights(
                "tomorrow_picks",
                db_path=config.VALIDATION_DB_PATH,
                top_k=10,
                days=days,
                steps=2,
                dry_run=False,
                direction_focus=direction_focus,
            )
            self.container.tomorrow_iteration.refresh_scoring_weights(written_result.get("weights") or {})
            payload = self.container.tomorrow_iteration.payload(
                written_result,
                applied=written_result.get("status") == "written",
                days=days,
            )
            self.container.tomorrow_iteration.save(payload)
            return self.json_payload(ok=True, iteration=payload)
        except Exception as exc:
            return self.error_payload(exc, iteration=self.container.tomorrow_iteration.load())

    def backtest_payload(
        self,
        *,
        raw_codes: str,
        top_k: int,
        holding_days: int,
        lookback_days: int,
        rebalance_step: int,
        mode: str,
    ) -> Tuple[Dict[str, object], int]:
        codes = parse_code_list(raw_codes)
        if not codes:
            codes = self.hooks.list_market_data_codes(config.MARKET_DATA_DB_PATH)[:500]
        if not codes:
            try:
                quotes = self._current_quotes()
                candidates, _ = self._candidates_with_regime_from_quotes(quotes)
                codes = candidates.sort_values(["pct_chg", "turnover"], ascending=False).head(40)["code"].tolist()
            except Exception:
                codes = parse_code_list("600000,000001,300750,688981")
        history_by_code = self.hooks.load_local_history_frames(codes[:500], days=160)
        for code in codes[:60]:
            if code in history_by_code:
                continue
            try:
                history = self.provider.get_history(code, days=160)
            except Exception:
                continue
            if history is not None and not history.empty:
                history_by_code[code] = history
        if mode == "snapshot":
            result = self.hooks.run_alphalite_backtest(
                history_by_code,
                top_k=top_k,
                holding_days=holding_days,
            )
        else:
            result = self.hooks.run_rolling_alphalite_backtest(
                history_by_code,
                top_k=top_k,
                holding_days=holding_days,
                lookback_days=lookback_days,
                rebalance_step=rebalance_step,
            )
        result["scope"] = "alphalite_research"
        result["production_strategy_validation"] = False
        result["warning"] = "独立 AlphaLite 研究回测，不代表明日优先或2-5日生产策略收益。"
        return result, 200

    def cached_metrics(self, strategy_name: str, days: int):
        return self.container.cached_metrics(strategy_name, days)

    def invalidate_metrics_cache(self) -> None:
        self.container.invalidate_metrics_cache()

    def cached_strategy_validation_summary(self, strategy_name: str, days: int):
        return self.container.cached_strategy_validation_summary(strategy_name, days)

    def research_disclaimer(self) -> str:
        return "仅供研究，不构成投资建议。"

    def json_payload(
        self,
        *,
        ok: bool,
        status: int = 200,
        include_health: bool = True,
        include_disclaimer: bool = False,
        **payload,
    ) -> Tuple[Dict[str, object], int]:
        body = response_payload(
            self.provider.health,
            self.research_disclaimer,
            ok=ok,
            include_health=include_health,
            include_disclaimer=include_disclaimer,
            **payload,
        )
        return body, status

    def error_payload(
        self,
        error,
        status: int = 502,
        include_disclaimer: bool = False,
        **payload,
    ) -> Tuple[Dict[str, object], int]:
        return self.json_payload(
            ok=False,
            status=status,
            error=str(error),
            include_disclaimer=include_disclaimer,
            **payload,
        )

    def bad_request_payload(self, message: str, **payload) -> Tuple[Dict[str, object], int]:
        return self.json_payload(ok=False, status=400, include_health=False, error=message, **payload)

    def _cached_hot_ranks(self) -> Dict[str, int]:
        return self.container.candidate_pipeline.hot_ranks()

    def _cached_industry_strength(self) -> Dict[str, float]:
        return self.container.candidate_pipeline.industry_strength()

    def _current_quotes(self) -> pd.DataFrame:
        return self.container.candidate_pipeline.current_quotes()

    def _current_quotes_or_empty(self) -> Tuple[pd.DataFrame, str]:
        return self.container.candidate_pipeline.current_quotes_or_empty()

    def _candidates_with_regime_from_quotes(
        self,
        quotes: pd.DataFrame,
        attach_codes=None,
    ) -> Tuple[pd.DataFrame, Dict[str, object]]:
        return self.container.candidate_pipeline.candidates_with_regime(quotes, attach_codes=attach_codes)

    def _recommendation_input_context(self) -> Dict[str, object]:
        return self.container.candidate_pipeline.recommendation_input_context()

    def _cached_market_sentiment(self) -> Dict[str, object]:
        cached = self.container.market_sentiment_cache.get()
        if cached is not None:
            return cached
        market_news = fetch_market_news(self.provider, self.container.market_news_cache)
        sentiment = build_market_sentiment_index(market_news)
        self.container.market_sentiment_cache.set(sentiment)
        return sentiment

    def _stability_update_locked(self, horizon: str, rows: List[Dict[str, object]]) -> Dict[str, object]:
        with self.container.recommendations_lock:
            return self.container.stability_tracker.update(horizon, rows)

    def _schedule_snapshot_save(self, payload: Dict[str, object]) -> None:
        if self._schedule_snapshot_save_callback is not None:
            self._schedule_snapshot_save_callback(payload)
            return
        self.container.snapshot_writer.schedule(payload)

    def _recommendation_cache_key(self, top_n: int, market: str) -> tuple:
        return self.recommendation_snapshots.recommendation_cache_key(top_n, market)

    @staticmethod
    def _is_executable_row(row: Dict[str, object]) -> bool:
        if not isinstance(row, dict):
            return False
        if row.get("execution_allowed") is False:
            return False
        action = row.get("trade_action")
        position_size = coerce_number((action or {}).get("position_size"), None) if isinstance(action, dict) else None
        if position_size is None:
            return True
        return float(position_size) > 0

    def _horizon_cache_key(self, strategy: str, top_n: int, market: str) -> tuple:
        return self.recommendation_snapshots.horizon_cache_key(strategy, top_n, market)

    def _remember_recommendation_payload(
        self,
        top_n: int,
        market: str,
        payload: Dict[str, object],
        *,
        source: str,
        stage: str = "ready",
        saved_at: str = "",
        saved_at_ts: float | None = None,
        _skip_frozen_lookup: bool = False,
    ) -> Dict[str, object]:
        return self.recommendation_snapshots.remember_recommendation_payload(
            top_n,
            market,
            payload,
            source=source,
            stage=stage,
            saved_at=saved_at,
            saved_at_ts=saved_at_ts,
            skip_frozen_lookup=_skip_frozen_lookup,
        )

    def _cached_recommendation_entry(self, top_n: int, market: str) -> Dict[str, object] | None:
        return self.recommendation_snapshots.cached_recommendation_entry(top_n, market)

    def _remember_horizon_payload(
        self,
        strategy: str,
        top_n: int,
        market: str,
        payload: Dict[str, object],
        *,
        saved_at: str = "",
        saved_at_ts: float | None = None,
        source: str = "live",
    ) -> Dict[str, object]:
        return self.recommendation_snapshots.remember_horizon_payload(
            strategy,
            top_n,
            market,
            payload,
            saved_at=saved_at,
            saved_at_ts=saved_at_ts,
            source=source,
        )

    def _cached_horizon_entry(self, strategy: str, top_n: int, market: str) -> Dict[str, object] | None:
        return self.recommendation_snapshots.cached_horizon_entry(strategy, top_n, market)

    def _snapshot_entry(self, top_n: int, market: str) -> Dict[str, object] | None:
        return self.recommendation_snapshots.snapshot_entry(top_n, market)

    def _safe_provider_health(self) -> Dict[str, object]:
        try:
            health = self.provider.health()
        except Exception as exc:
            return {"quotes_source": "unavailable", "errors": ["provider_health_failed: {}".format(exc)]}
        return dict(health) if isinstance(health, dict) else {"status": str(health)}

    def _provider_health_with_factor_coverage(
        self,
        candidates: pd.DataFrame = None,
        coverage: Dict[str, object] = None,
    ) -> Dict[str, object]:
        health = self._safe_provider_health()
        if coverage is None and candidates is not None:
            try:
                coverage = factor_coverage(candidates)
            except Exception:
                coverage = None
        if coverage is not None:
            health["factor_coverage"] = coverage
            coverage_alerts = coverage.get("alerts") if isinstance(coverage, dict) else []
            if coverage_alerts:
                alerts = list(health.get("alerts") or [])
                alerts.extend(coverage_alerts)
                health["alerts"] = alerts
        return health

    def _today_deepseek_api_call_count(self) -> int:
        if not self.validation_store:
            return 0
        try:
            today = datetime.now().strftime("%Y-%m-%d")
            with self.validation_store.repository.connect() as conn:
                row = conn.execute(
                    """
                    SELECT COALESCE(SUM(api_called), 0)
                    FROM deepseek_analysis_batches
                    WHERE substr(requested_at, 1, 10) = ?
                    """,
                    (today,),
                ).fetchone()
            return int(row[0] or 0)
        except Exception:
            return 0

    def _build_horizon_payload(self, strategy: str, top_n: int, market: str) -> Dict[str, object]:
        if recommendation_is_frozen():
            frozen = self._cached_horizon_entry(strategy, top_n, market)
            if frozen is not None:
                return dict(frozen.get("payload") or {})
            saved = self._saved_horizon_payload(strategy, top_n, market)
            if saved is not None:
                return saved
        _, candidates, market_regime = self._live_candidates_with_regime()
        coverage = factor_coverage(candidates)
        rows, meta, _ = scored_strategy_rows(
            strategy,
            candidates,
            top_n=top_n,
            market=market,
            market_regime=market_regime,
            apply_deepseek=False,
            validation_store=self.validation_store,
        )
        try:
            metrics = self.cached_metrics(strategy, validation_gate_window_days())
            apply_strategy_validation_gate(strategy, rows, meta, metrics)
        except Exception as exc:
            reason = "验证指标读取失败，暂停执行并仅保留备选：{}".format(exc)
            meta["validation_gate"] = {
                "state": "unavailable",
                "blocked": True,
                "allows_backup": True,
                "reason": reason,
            }
            demote_strategy_rows_to_backup(strategy, rows, meta, reason)
        rows, _ = apply_deepseek_to_reviewable_rows(
            strategy,
            rows,
            market,
            meta,
            validation_store=self.validation_store,
        )
        attach_validation_summary(rows, self.validation_store, strategy, metrics_fn=self.cached_metrics)
        meta["market_regime"] = market_regime
        meta["factor_coverage"] = coverage
        from ..production_baseline import attach_generation_provenance

        attach_generation_provenance(meta, strategy, rows, candidates)
        payload = response_payload(
            lambda: self._provider_health_with_factor_coverage(coverage=coverage),
            self.research_disclaimer,
            ok=True,
            include_disclaimer=True,
            data=rows,
            meta=meta,
        )
        return self._remember_horizon_payload(strategy, top_n, market, payload)["payload"]

    def _after_close_recommendations_payload(
        self,
        top_n: int,
        market: str,
    ) -> Dict[str, object] | None:
        signal_date = datetime.now().date().isoformat()
        strategies = list(self._configured_auto_snapshot_strategies())
        try:
            close_result = run_missing_close_snapshots(
                self.provider,
                self.validation_store,
                strategies,
                market=market,
            )
        except Exception as exc:
            message = "盘后推荐补缺失败，降级为已有推荐结果: {}".format(exc)
            _LOGGER.warning(message)
            append_error = getattr(self.provider, "append_status_error", None)
            if callable(append_error):
                append_error(message)
            return None
        rows_by_strategy: Dict[str, List[Dict[str, object]]] = {}
        phases: Dict[str, Dict[str, object]] = {}
        generated_times = []
        for strategy in strategies:
            batch = self.validation_store.saved_signal_batch(strategy, signal_date)
            if not batch:
                rows_by_strategy[strategy] = []
                continue
            phase = normalize_snapshot_phase(batch.get("snapshot_phase"))
            rows = self.validation_store.latest_signal_rows(
                strategy,
                signal_date=signal_date,
                snapshot_phase=phase,
            )
            rows_by_strategy[strategy] = rows[: max(0, int(top_n or 0))]
            phases[strategy] = phase_payload(phase, as_of=str(batch.get("signal_time") or ""))
            if batch.get("signal_time"):
                generated_times.append(str(batch["signal_time"]))
        if not any(rows_by_strategy.values()):
            return None
        distinct_phases = sorted({item["snapshot_phase"] for item in phases.values()})
        distinct_price_bases = sorted({item["price_basis"] for item in phases.values()})
        generated_at = max(generated_times) if generated_times else ""
        short_rows = rows_by_strategy.get("today_term") or []
        return {
            "ok": True,
            "data": short_rows,
            "recommendations": {
                "today_term": short_rows,
                "tomorrow_picks": rows_by_strategy.get("tomorrow_picks") or [],
                "swing_picks": rows_by_strategy.get("swing_picks") or [],
            },
            "meta": {
                "generated_at": generated_at,
                "as_of": generated_at,
                "signal_date": signal_date,
                "snapshot_phase": distinct_phases[0] if len(distinct_phases) == 1 else "mixed",
                "price_basis": distinct_price_bases[0] if len(distinct_price_bases) == 1 else "mixed",
                "phases": phases,
                "display_count": len(short_rows),
                "display_limit": top_n,
                "top_n": top_n,
                "market_filter": market,
                "close_snapshot": close_result,
            },
            "health": self._safe_provider_health(),
            "disclaimer": self.research_disclaimer(),
            "snapshot": {
                "saved_at": generated_at,
                "source": "validation_db_daily_snapshot",
                "stage": "ready",
                "snapshot_phase": distinct_phases[0] if len(distinct_phases) == 1 else "mixed",
                "as_of": generated_at,
                "price_basis": distinct_price_bases[0] if len(distinct_price_bases) == 1 else "mixed",
            },
        }

    def _live_candidates_with_regime(self, attach_codes=None) -> Tuple[pd.DataFrame, pd.DataFrame, Dict[str, object]]:
        quotes = self._current_quotes()
        candidates, market_regime = self._candidates_with_regime_from_quotes(quotes, attach_codes=attach_codes)
        return quotes, candidates, market_regime

    def _refresh_validation_rows_if_needed(
        self,
        rows: List[Dict[str, object]],
        *,
        signal_date: str,
        strategy_name: str,
        should_update: bool,
    ) -> Tuple[List[Dict[str, object]], object]:
        update_result = None
        needs_update = rows and any(
            not row.get("outcome_updated_at") and not row.get("skip_reason")
            for row in rows
        )
        if should_update and needs_update:
            update_result = self.validation_store.update_outcomes(
                self.provider,
                signal_date=signal_date,
                strategy_name=strategy_name,
            )
            self.invalidate_metrics_cache()
            rows = self.validation_store.signals_for_date(signal_date, strategy_name)
        return rows, update_result

    def _attach_validation_daily_quotes(self, rows: List[Dict[str, object]], include_quotes: bool) -> None:
        latest_quotes_by_code = {}
        if include_quotes:
            try:
                latest_quotes_by_code = quote_lookup(self._current_quotes())
            except Exception:
                latest_quotes_by_code = {}
        for row in rows:
            code = normalize_code(row.get("code", ""))
            quote = latest_quotes_by_code.get(code, {})
            current_price = coerce_number(quote.get("price"), None) if quote else None
            current_pct_chg = coerce_number(quote.get("pct_chg"), None) if quote else None
            signal_price = coerce_number(row.get("price_at_signal"), None)
            anchor_to_now_return = None
            if (
                current_price is not None
                and signal_price is not None
                and current_price > 0
                and signal_price > 0
            ):
                anchor_to_now_return = round((current_price / signal_price - 1) * 100, 4)
            row["current_price"] = current_price
            row["current_pct_chg"] = current_pct_chg
            row["anchor_to_now_return"] = anchor_to_now_return

    def _validation_batch_row(self, signal_date: str, strategy_name: str) -> Dict[str, object] | None:
        for row in self.validation_store.list_signal_dates(strategy_name):
            if str(row.get("signal_date") or "") == str(signal_date or "") and str(row.get("strategy_name") or "") == str(strategy_name or ""):
                return dict(row)
        return None

    def _configured_auto_snapshot_strategies(self) -> List[str]:
        return configured_auto_snapshot_strategies(DEFAULT_AUTO_SNAPSHOT_STRATEGIES, SNAPSHOT_STRATEGIES)

    def recommendation_is_frozen(self) -> bool:
        return recommendation_is_frozen()

    def _set_auto_update_status(self, **values) -> None:
        set_status(self.auto_update_lock, self.auto_update_status, **values)

    def _set_auto_snapshot_status(self, **values) -> None:
        set_status(self.auto_snapshot_lock, self.auto_snapshot_status, **values)

    def run_validation_tuning_once(
        self,
        strategies: List[str],
        days: int = 120,
    ) -> Dict[str, object]:
        return run_validation_tuning_once_support(
            self.validation_store,
            self.cached_metrics,
            strategies,
            days=days,
        )

    def run_validation_auto_snapshot_once(self) -> Dict[str, object]:
        return run_validation_auto_snapshot_once_support(
            normalize_market=normalize_market,
            provider=self.provider,
            validation_store=self.validation_store,
            auto_snapshot_lock=self.auto_snapshot_lock,
            auto_snapshot_status=self.auto_snapshot_status,
            configured_auto_snapshot_strategies_fn=self._configured_auto_snapshot_strategies,
            run_snapshots_fn=run_snapshots,
            invalidate_metrics_cache=self.invalidate_metrics_cache,
            run_validation_tuning_once_fn=self.run_validation_tuning_once,
            set_auto_snapshot_status=self._set_auto_snapshot_status,
        )

    def run_validation_auto_update_once(self) -> Dict[str, object]:
        def update_incomplete_outcomes() -> Dict[str, object]:
            updates = []
            ok = True
            for strategy in self._configured_auto_snapshot_strategies():
                try:
                    outcome = self.validation_store.update_outcomes(
                        self.provider,
                        strategy_name=strategy,
                        only_incomplete=True,
                    )
                    updates.append({"strategy": strategy, "result": outcome})
                except Exception as exc:
                    ok = False
                    updates.append({"strategy": strategy, "error": str(exc)})
            self.invalidate_metrics_cache()
            return {"ok": ok, "updates": updates}

        def generate_oos_reports() -> Dict[str, object]:
            reports = []
            ok = True
            days = validation_gate_window_days()
            for strategy in self._configured_auto_snapshot_strategies():
                try:
                    if strategy in set(getattr(config, "PORTFOLIO_BASELINE_STRATEGIES", ("tomorrow_picks",))):
                        from ..portfolio_baseline import DailyPortfolioBaselineService

                        DailyPortfolioBaselineService(self.validation_store).run(
                            self.provider,
                            strategy,
                            days=min(days, 6),
                            reuse_settled=True,
                        )
                    report = generate_strategy_oos_report(
                        self.validation_store,
                        strategy,
                        days,
                        strategy_validation_gate_decision,
                    )
                    saved = self.validation_store.save_oos_report(report, trigger="auto_update")
                    reports.append(
                        {
                            "strategy": strategy,
                            "oos_status": report.get("oos_status"),
                            "report": report,
                            "saved": saved,
                        }
                    )
                except Exception as exc:
                    ok = False
                    reports.append({"strategy": strategy, "error": str(exc)})
            return {"ok": ok, "reports": reports}

        return run_validation_auto_update_once_support(
            auto_update_lock=self.auto_update_lock,
            auto_update_status=self.auto_update_status,
            set_auto_update_status=self._set_auto_update_status,
            run_validation_outcome_update_once_fn=update_incomplete_outcomes,
            run_oos_reports_once_fn=generate_oos_reports,
        )

    def _build_recommendations_payload(self, top_n: int, market: str, include_deepseek: bool = True) -> tuple:
        if recommendation_is_frozen():
            frozen = self._cached_recommendation_entry(top_n, market) or self._snapshot_entry(top_n, market)
            if frozen is not None:
                return self._serve_recommendation_payload(frozen), 200
        try:
            blacklist_payload = load_risk_blacklist()
            context = self._recommendation_input_context()
            quotes = context["quotes"]
            hard_filter_report = context["hard_filter_report"]
            candidates = context["candidates"]
            market_regime = context["market_regime"]
            hot_ranks = context["hot_ranks"]
            industry_strength = context["industry_strength"]
            sentiment_lookup = context["sentiment_lookup"]
            coverage = factor_coverage(candidates)

            recommendations_by_horizon, meta, deepseek_meta_by_strategy = build_recommendation_horizons(
                candidates,
                top_n,
                market,
                market_regime,
                hot_ranks,
                industry_strength,
                sentiment_lookup,
                self.cached_metrics,
                apply_deepseek=include_deepseek,
                validation_store=self.validation_store,
            )
            short_display_rows, meta = finalize_recommendation_payload_meta(
                recommendations_by_horizon["today_term"],
                meta,
                blacklist_payload,
                hard_filter_report,
                market_regime,
                deepseek_meta_by_strategy,
                top_n,
                self._stability_update_locked,
                self.validation_store,
                self.cached_metrics,
            )
            meta["factor_coverage"] = coverage
            short_display_count = len(short_display_rows)
            short_executable_count = sum(1 for row in short_display_rows if self._is_executable_row(row))
            meta["display_count"] = short_display_count
            meta["short_term_executable_count"] = short_executable_count
            meta["short_term_observation_count"] = short_display_count - short_executable_count
            meta["today_term_executable_count"] = short_executable_count
            meta["today_term_observation_count"] = short_display_count - short_executable_count
            meta["deepseek_api_call_count"] = self._today_deepseek_api_call_count()
            meta["quote_timestamp"] = str(
                (getattr(quotes, "attrs", {}) or {}).get("quote_timestamp") or ""
            )
            from ..production_baseline import attach_generation_provenance

            attach_generation_provenance(meta, "today_term", short_display_rows, candidates)
            recommendations_by_horizon["today_term"] = short_display_rows
            payload = {
                "ok": True,
                "data": short_display_rows,
                "recommendations": recommendations_by_horizon,
                "meta": meta,
                "market_sentiment": self._cached_market_sentiment(),
                "health": self._provider_health_with_factor_coverage(coverage=coverage),
                "disclaimer": self.research_disclaimer(),
            }
            self._schedule_snapshot_save(payload)
            self._remember_recommendation_payload(
                top_n,
                market,
                payload,
                source="live",
                stage="ready" if include_deepseek else "local_only",
            )
            return payload, 200
        except Exception as exc:
            snapshot = load_recommendation_snapshot(
                config.RECOMMENDATION_SNAPSHOT_PATH,
                max_age_seconds=getattr(config, "RECOMMENDATION_SNAPSHOT_MAX_AGE_SECONDS", 300),
                expected_market=market,
                expected_top_n=top_n,
            )
            if snapshot.get("ok"):
                return snapshot_fallback_payload(snapshot, exc), 200
            return error_payload(self.provider.health, self.research_disclaimer, exc), 502

    def _recommendation_snapshot_info(self, entry: Dict[str, object]) -> Dict[str, object]:
        return self.recommendation_snapshots.snapshot_info(entry)

    def _serve_recommendation_payload(self, entry: Dict[str, object]) -> Dict[str, object]:
        return self.recommendation_snapshots.serve_recommendation_payload(entry)

    def _overlay_live_quotes_on_payload(self, payload: Dict[str, object]) -> Dict[str, object]:
        if not isinstance(payload, dict) or not payload.get("ok"):
            return payload
        recommendations = dict(payload.get("recommendations") or {})
        payload_rows = list(payload.get("data") or [])
        all_rows = list(payload_rows)
        for rows in recommendations.values():
            if isinstance(rows, list):
                all_rows.extend(rows)
        codes = sorted(
            {
                normalize_code(row.get("code"))
                for row in all_rows
                if isinstance(row, dict) and normalize_code(row.get("code"))
            }
        )
        quotes, quote_error = self.container.candidate_pipeline.recommendation_quotes(codes)
        quote_scope = "recommendation_pool"
        if quotes is None or quotes.empty:
            quotes, fallback_error = self._current_quotes_or_empty()
            quote_error = quote_error or fallback_error
            quote_scope = "full_market_fallback"
        if quotes is None or quotes.empty:
            return payload
        live_by_code = quote_lookup(quotes)
        full_quotes = self.container.quotes_cache.get()
        full_by_code = quote_lookup(full_quotes) if full_quotes is not None and not full_quotes.empty else {}
        full_timestamp = str((getattr(full_quotes, "attrs", {}) or {}).get("quote_timestamp") or "")
        row_quality = {}
        dynamic_fields = (
            "price",
            "pct_chg",
            "speed",
            "five_min_pct",
            "volume_ratio",
            "turnover_rate",
            "turnover",
            "volume",
            "amplitude",
            "high",
            "low",
            "open",
        )

        def timestamp_age_seconds(value):
            if not value:
                return None
            try:
                return round(max(0.0, (datetime.now() - datetime.fromisoformat(str(value))).total_seconds()), 2)
            except ValueError:
                return None

        def overlay(rows):
            result = []
            for source_row in rows or []:
                row = dict(source_row)
                code = normalize_code(row.get("code"))
                targeted_quote = live_by_code.get(code, {}) if quote_scope == "recommendation_pool" else {}
                fallback_quote = full_by_code.get(code, {})
                quote = targeted_quote or live_by_code.get(code, {}) or fallback_quote
                for field in dynamic_fields:
                    if quote.get(field) is not None:
                        row[field] = quote[field]
                if targeted_quote:
                    for unavailable_field in ("speed", "five_min_pct"):
                        if quote.get(unavailable_field) is None:
                            row[unavailable_field] = None
                row_timestamp = str(
                    quote.get("quote_timestamp")
                    or ((getattr(quotes, "attrs", {}) or {}).get("quote_timestamp") if targeted_quote else full_timestamp)
                    or ""
                )
                row_source = str(
                    quote.get("quote_source")
                    or ((getattr(quotes, "attrs", {}) or {}).get("quote_source") if targeted_quote else "全市场快照回退")
                    or ""
                )
                row_age = timestamp_age_seconds(row_timestamp)
                stale_seconds = max(1, int(getattr(config, "RECOMMENDATION_QUOTE_STALE_SECONDS", 30)))
                now = datetime.now()
                minutes = now.hour * 60 + now.minute
                market_active = now.weekday() < 5 and (
                    565 <= minutes <= 690 or 780 <= minutes <= 910
                )
                row_stale = bool(market_active and (row_age is None or row_age > stale_seconds))
                source_deviation_pct = None
                source_mismatch = False
                primary_price = quote.get("price") if targeted_quote else None
                fallback_price = fallback_quote.get("price")
                full_age = timestamp_age_seconds(full_timestamp)
                if primary_price and fallback_price and full_age is not None and full_age <= 60:
                    source_deviation_pct = round(abs(float(primary_price) - float(fallback_price)) / abs(float(primary_price)) * 100.0, 4)
                    source_mismatch = source_deviation_pct > float(
                        getattr(config, "RECOMMENDATION_QUOTE_MAX_SOURCE_DEVIATION_PCT", 0.5)
                    )
                row["quote_timestamp"] = row_timestamp
                row["quote_source"] = row_source
                row["quote_age_seconds"] = row_age
                row["quote_stale"] = row_stale
                row["quote_source_deviation_pct"] = source_deviation_pct
                row["quote_source_mismatch"] = source_mismatch
                row_quality[code] = {
                    "timestamp": row_timestamp,
                    "age": row_age,
                    "stale": row_stale,
                    "source": row_source,
                    "mismatch": source_mismatch,
                }
                result.append(row)
            return result

        for strategy, rows in list(recommendations.items()):
            if isinstance(rows, list):
                recommendations[strategy] = overlay(rows)
        payload["recommendations"] = recommendations
        if isinstance(payload.get("data"), list):
            payload["data"] = overlay(payload["data"])
        health = {**dict(payload.get("health") or {}), **self._safe_provider_health()}
        full_market_source = health.get("quotes_source")
        quote_timestamp = str(
            (getattr(quotes, "attrs", {}) or {}).get("quote_timestamp")
            or health.get("last_quote_refresh")
            or ""
        )
        quote_source = str((getattr(quotes, "attrs", {}) or {}).get("quote_source") or full_market_source or "")
        ages = sorted(float(item["age"]) for item in row_quality.values() if item.get("age") is not None)
        quote_age_seconds = max(ages) if ages else timestamp_age_seconds(quote_timestamp)
        p95_index = min(len(ages) - 1, max(0, int(len(ages) * 0.95))) if ages else 0
        quote_p95_age_seconds = ages[p95_index] if ages else quote_age_seconds
        stale_codes = sorted(code for code, item in row_quality.items() if item.get("stale"))
        mismatch_codes = sorted(code for code, item in row_quality.items() if item.get("mismatch"))
        missing_codes = sorted((getattr(quotes, "attrs", {}) or {}).get("missing_codes") or [])
        if quote_scope == "recommendation_pool" and missing_codes:
            quote_scope = "mixed"
        quote_version = "|".join(
            f"{code}:{item.get('timestamp') or 'missing'}"
            for code, item in sorted(row_quality.items())
        )
        health["full_market_quotes_source"] = full_market_source
        health["recommendation_quotes_source"] = quote_source
        health["recommendation_quote_error"] = quote_error
        health["recommendation_quote_missing_codes"] = missing_codes
        health["recommendation_quote_stale_codes"] = stale_codes
        health["recommendation_quote_source_mismatch_codes"] = mismatch_codes
        health["quotes_source"] = quote_source
        health["last_quote_refresh"] = quote_timestamp
        meta = dict(payload.get("meta") or {})
        meta["quote_timestamp"] = quote_timestamp
        meta["quote_age_seconds"] = quote_age_seconds
        meta["quote_p95_age_seconds"] = quote_p95_age_seconds
        meta["quote_stale_count"] = len(stale_codes)
        meta["quote_missing_count"] = len(missing_codes)
        meta["quote_source_mismatch_count"] = len(mismatch_codes)
        meta["quote_version"] = quote_version
        meta["quote_scope"] = quote_scope
        meta["quote_overlay_at"] = datetime.now().isoformat(timespec="seconds")
        payload["meta"] = meta
        payload["health"] = health
        return payload

    def _refresh_recommendation_cache(self, top_n: int, market: str) -> None:
        return self.recommendation_refresh.refresh_recommendation_cache(top_n, market)

    def _schedule_recommendation_refresh(self, top_n: int, market: str) -> bool:
        return self.recommendation_refresh.schedule_recommendation_refresh(top_n, market)

    def _refresh_horizon_cache(self, strategy: str, top_n: int, market: str) -> None:
        return self.recommendation_refresh.refresh_horizon_cache(strategy, top_n, market)

    def _schedule_horizon_refresh(self, strategy: str, top_n: int, market: str) -> bool:
        return self.recommendation_refresh.schedule_horizon_refresh(strategy, top_n, market)

    def _saved_horizon_payload(
        self,
        strategy: str,
        top_n: int,
        market: str,
        signal_date: str = "",
    ) -> Dict[str, object] | None:
        batch = self.validation_store.saved_signal_batch(strategy, signal_date) if signal_date else {}
        phase = normalize_snapshot_phase(batch.get("snapshot_phase")) if batch else ""
        saved_rows = self.validation_store.latest_signal_rows(
            strategy,
            signal_date=signal_date,
            snapshot_phase=phase,
        )
        if not saved_rows:
            return None
        if strategy == "tomorrow_picks":
            payload = saved_tomorrow_fallback_payload(
                saved_rows=saved_rows,
                top_n=top_n,
                market=market,
                detailed=True,
                validation_store=self.validation_store,
                cached_metrics_fn=self.cached_metrics,
                load_risk_blacklist_fn=load_risk_blacklist,
                analysis_window_fn=lambda: analysis_window(config.VALIDATION_AUTO_SNAPSHOT_TIME),
                provider_health_fn=self.provider.health,
                research_disclaimer_fn=self.research_disclaimer,
            )
        else:
            payload = saved_swing_fallback_payload(
                saved_rows=saved_rows,
                top_n=top_n,
                market=market,
                validation_store=self.validation_store,
                cached_metrics_fn=self.cached_metrics,
                provider_health_fn=self.provider.health,
                research_disclaimer_fn=self.research_disclaimer,
            )
        meta = dict(payload.get("meta") or {})
        if batch:
            phase_meta = phase_payload(phase, as_of=str(batch.get("signal_time") or ""))
            meta.update(phase_meta)
            meta["generated_at"] = str(batch.get("signal_time") or "")
            meta["signal_date"] = str(batch.get("signal_date") or signal_date)
        payload["meta"] = meta
        snapshot = dict(payload.get("snapshot") or {})
        if batch:
            snapshot.update(
                {
                    "saved_at": str(batch.get("signal_time") or ""),
                    "source": snapshot.get("source") or "validation_db_daily_snapshot",
                    "stage": snapshot.get("stage") or "ready",
                    "snapshot_phase": meta.get("snapshot_phase"),
                    "as_of": meta.get("as_of"),
                    "price_basis": meta.get("price_basis"),
                }
            )
            payload["snapshot"] = snapshot
        return payload

    def _horizon_payload(self, strategy: str, top_n: int, market: str) -> tuple:
        refresh_after_seconds = max(5, int(config.REFRESH_SECONDS))
        entry = self._cached_horizon_entry(strategy, top_n, market)
        if entry is not None:
            age_seconds = max(0.0, time.time() - float(entry.get("saved_at_ts") or 0.0))
            payload = dict(entry.get("payload") or {})
            payload["snapshot"] = {
                "saved_at": entry.get("saved_at", ""),
                "age_seconds": round(age_seconds, 2),
                "source": entry.get("source", "memory_cache"),
            }
            if age_seconds >= refresh_after_seconds:
                self._schedule_horizon_refresh(strategy, top_n, market)
            return payload, 200
        self._schedule_horizon_refresh(strategy, top_n, market)
        payload = self._saved_horizon_payload(strategy, top_n, market)
        if payload is not None:
            payload["snapshot"] = {
                "saved_at": "",
                "age_seconds": None,
                "source": "saved_snapshot",
            }
            return payload, 200
        return response_payload(
            self.provider.health,
            self.research_disclaimer,
            ok=True,
            include_disclaimer=True,
            data=[],
            meta={
                "generated_at": "",
                "candidate_count": 0,
                "display_count": 0,
                "display_limit": top_n,
                "top_n": top_n,
                "market_filter": market,
                "strategy_label": "明日优先" if strategy == "tomorrow_picks" else "2-5日持有",
                "strategy": "后台刷新中",
                "fallback": "async_refresh_pending",
            },
            snapshot={"saved_at": "", "age_seconds": None, "source": "async_refresh_pending"},
        ), 200

    def _prediction_strategy_rows(self, *args, **kwargs):
        return build_prediction_strategy_rows(*args, **kwargs)


class _UseCase:
    def __init__(self, context: _AppServiceContext) -> None:
        self.context = context


class RecommendationUseCase(_UseCase):
    def index_context(self) -> Dict[str, object]:
        return self.context.index_context()

    def empty_stream_status(self) -> Tuple[Dict[str, str], int]:
        return self.context.empty_stream_status()

    def recommendations_payload(self, top_n: int, market: str) -> Tuple[Dict[str, object], int]:
        return self.context.recommendations_payload(top_n, market)

    def latest_recommendations_payload(
        self,
        top_n: int,
        market: str,
        max_age: int,
    ) -> Tuple[Dict[str, object], int]:
        return self.context.latest_recommendations_payload(top_n, market, max_age)

    def horizon_payload(self, strategy: str, top_n: int, market: str) -> Tuple[Dict[str, object], int]:
        return self.context.horizon_payload(strategy, top_n, market)


class PredictionUseCase(_UseCase):
    def stock_prediction_payload(self, code: str) -> Tuple[Dict[str, object], int]:
        return self.context.stock_prediction_payload(code)

    def stock_prediction_stance_validation(self, days: int) -> Tuple[Dict[str, object], int]:
        return self.context.stock_prediction_stance_validation(days)

    def stock_prediction_stance_validation_update(self, days: int) -> Tuple[Dict[str, object], int]:
        return self.context.stock_prediction_stance_validation_update(days)


class ValidationUseCase(_UseCase):
    def strategy_snapshot(self, strategy: str, market: str) -> Tuple[Dict[str, object], int]:
        return self.context.strategy_snapshot(strategy, market)

    def strategy_validation_update(self, signal_date: str, strategy: str) -> Tuple[Dict[str, object], int]:
        return self.context.strategy_validation_update(signal_date, strategy)

    def strategy_validation_auto_update_status(self) -> Tuple[Dict[str, object], int]:
        return self.context.strategy_validation_auto_update_status()

    def strategy_validation_prefetch_history(
        self,
        *,
        signal_date: str,
        strategy: str,
        days: int,
        limit: int,
        force: bool,
        update: bool,
    ) -> Tuple[Dict[str, object], int]:
        return self.context.strategy_validation_prefetch_history(
            signal_date=signal_date,
            strategy=strategy,
            days=days,
            limit=limit,
            force=force,
            update=update,
        )

    def strategy_validation_backfill_current_baseline(
        self,
        *,
        strategy: str,
        days: int,
        history_days: int,
        limit: int,
        force: bool,
        execute: bool,
    ) -> Tuple[Dict[str, object], int]:
        return self.context.strategy_validation_backfill_current_baseline(
            strategy=strategy,
            days=days,
            history_days=history_days,
            limit=limit,
            force=force,
            execute=execute,
        )

    def strategy_validation_oos_report(self, strategy: str, days: int) -> Tuple[Dict[str, object], int]:
        return self.context.strategy_validation_oos_report(strategy, days)

    def strategy_validation_oos_report_history(self, strategy: str, limit: int) -> Tuple[Dict[str, object], int]:
        return self.context.strategy_validation_oos_report_history(strategy, limit)

    def strategy_validation_readiness(self) -> Tuple[Dict[str, object], int]:
        return self.context.strategy_validation_readiness()

    def strategy_validation_portfolio_baseline(self, **kwargs) -> Tuple[Dict[str, object], int]:
        return self.context.strategy_validation_portfolio_baseline(**kwargs)

    def strategy_validation_backfill_samples(
        self,
        *,
        strategy: str,
        days: int,
        replay_days: int,
        top_n: int,
        holding_days: int,
        limit: int,
        force: bool,
    ) -> Tuple[Dict[str, object], int]:
        return self.context.strategy_validation_backfill_samples(
            strategy=strategy,
            days=days,
            replay_days=replay_days,
            top_n=top_n,
            holding_days=holding_days,
            limit=limit,
            force=force,
        )

    def strategy_validation(self, *, strategy: str, days: int, light: bool) -> Tuple[Dict[str, object], int]:
        return self.context.strategy_validation(strategy=strategy, days=days, light=light)

    def strategy_validation_runtime_config(self, strategy: str, days: int) -> Tuple[Dict[str, object], int]:
        return self.context.strategy_validation_runtime_config(strategy, days)

    def strategy_validation_tuning(
        self,
        *,
        strategy: str,
        days: int,
        method: str,
    ) -> Tuple[Dict[str, object], int]:
        return self.context.strategy_validation_tuning(
            strategy=strategy,
            days=days,
            method=method,
        )

    def strategy_validation_daily(
        self,
        *,
        signal_date: str,
        strategy: str,
        should_update: bool,
        include_quotes: bool,
    ) -> Tuple[Dict[str, object], int]:
        return self.context.strategy_validation_daily(
            signal_date=signal_date,
            strategy=strategy,
            should_update=should_update,
            include_quotes=include_quotes,
        )

    def tomorrow_iteration(self, *, days: int, force: bool, direction_focus) -> Tuple[Dict[str, object], int]:
        return self.context.tomorrow_iteration(days=days, force=force, direction_focus=direction_focus)

    def tomorrow_iteration_apply(self, *, days: int, direction_focus) -> Tuple[Dict[str, object], int]:
        return self.context.tomorrow_iteration_apply(days=days, direction_focus=direction_focus)


class BacktestUseCase(_UseCase):
    def backtest_payload(
        self,
        *,
        raw_codes: str,
        top_k: int,
        holding_days: int,
        lookback_days: int,
        rebalance_step: int,
        mode: str,
    ) -> Tuple[Dict[str, object], int]:
        return self.context.backtest_payload(
            raw_codes=raw_codes,
            top_k=top_k,
            holding_days=holding_days,
            lookback_days=lookback_days,
            rebalance_step=rebalance_step,
            mode=mode,
        )


class HealthUseCase(_UseCase):
    def health_payload(self) -> Tuple[Dict[str, object], int]:
        return self.context.health_payload()


class BackgroundWorkerService(_UseCase):
    def start_background_workers(self) -> bool:
        return self.context.start_background_workers()

    def stop_background_workers(self, timeout_seconds: float = 5.0) -> None:
        self.context.stop_background_workers(timeout_seconds)

    def run_validation_tuning_once(
        self,
        strategies: List[str],
        days: int = 120,
    ) -> Dict[str, object]:
        return self.context.run_validation_tuning_once(strategies, days=days)

    def run_validation_auto_snapshot_once(self) -> Dict[str, object]:
        return self.context.run_validation_auto_snapshot_once()

    def run_validation_auto_update_once(self) -> Dict[str, object]:
        return self.context.run_validation_auto_update_once()


class AppServices:
    """Facade and composition root for route-facing application use cases."""

    def __init__(self, container, hooks: AppServiceHooks) -> None:
        self.context = _AppServiceContext(
            container,
            hooks,
            schedule_snapshot_save=self._schedule_snapshot_save,
        )
        self.container = container
        self.hooks = hooks
        self.provider = self.context.provider
        self.validation_store = self.context.validation_store
        self.recommendations = RecommendationUseCase(self.context)
        self.predictions = PredictionUseCase(self.context)
        self.validation = ValidationUseCase(self.context)
        self.backtests = BacktestUseCase(self.context)
        self.health = HealthUseCase(self.context)
        self.background_workers = BackgroundWorkerService(self.context)

    def start_validation_workers(self) -> bool:
        return self.background_workers.start_background_workers()

    def stop_validation_workers(self, timeout_seconds: float = 5.0) -> None:
        self.background_workers.stop_background_workers(timeout_seconds)

    def stop_recommendation_refresh_workers(self, timeout_seconds: float = 5.0) -> None:
        self.context.recommendation_refresh.stop(timeout_seconds)

    def stop_transient_workers(self, timeout_seconds: float = 5.0) -> None:
        self.stop_recommendation_refresh_workers(timeout_seconds)
        stop_realtime_quotes = getattr(self.provider, "stop_realtime_quotes", None)
        if callable(stop_realtime_quotes):
            stop_realtime_quotes(timeout_seconds)
        self.container.snapshot_writer.stop(timeout_seconds)

    def start_background_workers(self) -> bool:
        realtime_started = self.context.container.realtime_scheduler.start()
        validation_started = self.start_validation_workers()
        return bool(realtime_started or validation_started)

    def stop_background_workers(self, timeout_seconds: float = 5.0) -> None:
        self.stop_transient_workers(timeout_seconds)
        self.stop_validation_workers(timeout_seconds)
        self.context.container.realtime_scheduler.stop(timeout_seconds)

    def index_context(self) -> Dict[str, object]:
        return self.recommendations.index_context()

    def empty_stream_status(self) -> Tuple[Dict[str, str], int]:
        return self.recommendations.empty_stream_status()

    def health_payload(self) -> Tuple[Dict[str, object], int]:
        return self.health.health_payload()

    def recommendations_payload(self, top_n: int, market: str) -> Tuple[Dict[str, object], int]:
        return self.recommendations.recommendations_payload(top_n, market)

    def latest_recommendations_payload(
        self,
        top_n: int,
        market: str,
        max_age: int,
    ) -> Tuple[Dict[str, object], int]:
        return self.recommendations.latest_recommendations_payload(top_n, market, max_age)

    def horizon_payload(self, strategy: str, top_n: int, market: str) -> Tuple[Dict[str, object], int]:
        return self.recommendations.horizon_payload(strategy, top_n, market)

    def stock_prediction_payload(self, code: str) -> Tuple[Dict[str, object], int]:
        return self.predictions.stock_prediction_payload(code)

    def stock_prediction_stance_validation(self, days: int) -> Tuple[Dict[str, object], int]:
        return self.predictions.stock_prediction_stance_validation(days)

    def stock_prediction_stance_validation_update(self, days: int) -> Tuple[Dict[str, object], int]:
        return self.predictions.stock_prediction_stance_validation_update(days)

    def strategy_snapshot(self, strategy: str, market: str) -> Tuple[Dict[str, object], int]:
        return self.validation.strategy_snapshot(strategy, market)

    def strategy_validation_update(self, signal_date: str, strategy: str) -> Tuple[Dict[str, object], int]:
        return self.validation.strategy_validation_update(signal_date, strategy)

    def strategy_validation_auto_update_status(self) -> Tuple[Dict[str, object], int]:
        return self.validation.strategy_validation_auto_update_status()

    def strategy_validation_prefetch_history(
        self,
        *,
        signal_date: str,
        strategy: str,
        days: int,
        limit: int,
        force: bool,
        update: bool,
    ) -> Tuple[Dict[str, object], int]:
        return self.validation.strategy_validation_prefetch_history(
            signal_date=signal_date,
            strategy=strategy,
            days=days,
            limit=limit,
            force=force,
            update=update,
        )

    def strategy_validation_backfill_current_baseline(
        self,
        *,
        strategy: str,
        days: int,
        history_days: int,
        limit: int,
        force: bool,
        execute: bool,
    ) -> Tuple[Dict[str, object], int]:
        return self.validation.strategy_validation_backfill_current_baseline(
            strategy=strategy,
            days=days,
            history_days=history_days,
            limit=limit,
            force=force,
            execute=execute,
        )

    def strategy_validation_oos_report(self, strategy: str, days: int) -> Tuple[Dict[str, object], int]:
        return self.validation.strategy_validation_oos_report(strategy, days)

    def strategy_validation_oos_report_history(self, strategy: str, limit: int) -> Tuple[Dict[str, object], int]:
        return self.validation.strategy_validation_oos_report_history(strategy, limit)

    def strategy_validation_readiness(self) -> Tuple[Dict[str, object], int]:
        return self.validation.strategy_validation_readiness()

    def strategy_validation_portfolio_baseline(self, **kwargs) -> Tuple[Dict[str, object], int]:
        return self.validation.strategy_validation_portfolio_baseline(**kwargs)

    def strategy_validation_backfill_samples(
        self,
        *,
        strategy: str,
        days: int,
        replay_days: int,
        top_n: int,
        holding_days: int,
        limit: int,
        force: bool,
    ) -> Tuple[Dict[str, object], int]:
        return self.validation.strategy_validation_backfill_samples(
            strategy=strategy,
            days=days,
            replay_days=replay_days,
            top_n=top_n,
            holding_days=holding_days,
            limit=limit,
            force=force,
        )

    def strategy_validation(self, *, strategy: str, days: int, light: bool) -> Tuple[Dict[str, object], int]:
        return self.validation.strategy_validation(strategy=strategy, days=days, light=light)

    def strategy_validation_runtime_config(self, strategy: str, days: int) -> Tuple[Dict[str, object], int]:
        return self.validation.strategy_validation_runtime_config(strategy, days)

    def strategy_validation_tuning(
        self,
        *,
        strategy: str,
        days: int,
        method: str,
    ) -> Tuple[Dict[str, object], int]:
        return self.validation.strategy_validation_tuning(
            strategy=strategy,
            days=days,
            method=method,
        )

    def strategy_validation_daily(
        self,
        *,
        signal_date: str,
        strategy: str,
        should_update: bool,
        include_quotes: bool,
    ) -> Tuple[Dict[str, object], int]:
        return self.validation.strategy_validation_daily(
            signal_date=signal_date,
            strategy=strategy,
            should_update=should_update,
            include_quotes=include_quotes,
        )

    def tomorrow_iteration(self, *, days: int, force: bool, direction_focus) -> Tuple[Dict[str, object], int]:
        return self.validation.tomorrow_iteration(days=days, force=force, direction_focus=direction_focus)

    def tomorrow_iteration_apply(self, *, days: int, direction_focus) -> Tuple[Dict[str, object], int]:
        return self.validation.tomorrow_iteration_apply(days=days, direction_focus=direction_focus)

    def backtest_payload(
        self,
        *,
        raw_codes: str,
        top_k: int,
        holding_days: int,
        lookback_days: int,
        rebalance_step: int,
        mode: str,
    ) -> Tuple[Dict[str, object], int]:
        return self.backtests.backtest_payload(
            raw_codes=raw_codes,
            top_k=top_k,
            holding_days=holding_days,
            lookback_days=lookback_days,
            rebalance_step=rebalance_step,
            mode=mode,
        )

    def run_validation_tuning_once(
        self,
        strategies: List[str],
        days: int = 120,
    ) -> Dict[str, object]:
        return self.background_workers.run_validation_tuning_once(strategies, days=days)

    def run_validation_auto_snapshot_once(self) -> Dict[str, object]:
        return self.background_workers.run_validation_auto_snapshot_once()

    def run_validation_auto_update_once(self) -> Dict[str, object]:
        return self.background_workers.run_validation_auto_update_once()

    def cached_metrics(self, strategy_name: str, days: int):
        return self.context.cached_metrics(strategy_name, days)

    def invalidate_metrics_cache(self) -> None:
        self.context.invalidate_metrics_cache()

    def cached_strategy_validation_summary(self, strategy_name: str, days: int):
        return self.context.cached_strategy_validation_summary(strategy_name, days)

    def research_disclaimer(self) -> str:
        return self.context.research_disclaimer()

    def _schedule_snapshot_save(self, payload: Dict[str, object]) -> None:
        self.container.snapshot_writer.schedule(payload)


def normalize_market(value: str) -> str:
    text = str(value or "").strip().lower().replace(" ", "")
    if text in ("all", "main", "chinext", "star"):
        return text
    return "all"


def normalize_validation_strategy(raw_strategy: str = "", default: str = "today_term") -> str:
    strategy = storage_strategy_name(raw_strategy or default)
    if strategy not in SNAPSHOT_STRATEGIES:
        strategy = default if default in SNAPSHOT_STRATEGIES else "today_term"
    return strategy


def normalize_optional_validation_strategy(raw_strategy: str = "") -> str:
    strategy = storage_strategy_name(str(raw_strategy or "").strip()) if raw_strategy else ""
    if strategy and strategy not in SNAPSHOT_STRATEGIES:
        return ""
    return strategy
