from datetime import datetime, timedelta
import threading
import time
from typing import Callable, Dict, List, Tuple

from . import config
from .strategy_tuning import build_strategy_tuning_plan
from .validation_backup import backup_validation_db


def configured_auto_snapshot_strategies(
    default_snapshot_strategies,
    snapshot_strategies,
) -> List[str]:
    raw = str(getattr(config, "VALIDATION_AUTO_SNAPSHOT_STRATEGIES", "") or "").strip()
    if not raw:
        strategies = list(default_snapshot_strategies)
        return _prioritize_deadline_strategy(strategies)
    requested = [item.strip() for item in raw.replace("，", ",").split(",") if item.strip()]
    if any(item.lower() == "all" for item in requested):
        return _prioritize_deadline_strategy(list(default_snapshot_strategies))
    strategies = [item for item in requested if item in snapshot_strategies]
    return _prioritize_deadline_strategy(strategies or ["tomorrow_picks"])


def _prioritize_deadline_strategy(strategies: List[str]) -> List[str]:
    unique = list(dict.fromkeys(strategies))
    if "tomorrow_picks" in unique:
        unique.remove("tomorrow_picks")
        unique.insert(0, "tomorrow_picks")
    return unique


def analysis_window(snapshot_time: str) -> str:
    raw = str(snapshot_time or "14:50").strip() or "14:50"
    if ":" not in raw:
        return "14:50"
    try:
        hour_text, minute_text = raw.split(":", 1)
        hour = max(0, min(23, int(hour_text)))
        minute = max(0, min(59, int(minute_text)))
        return "{:02d}:{:02d}".format(hour, minute)
    except Exception:
        return "14:50"


def set_status(lock, status: Dict[str, object], **values) -> None:
    with lock:
        status.update(values)


def auto_snapshot_time_parts(snapshot_time: str) -> Tuple[int, int]:
    raw = str(snapshot_time or "14:50").strip()
    try:
        hour_text, minute_text = raw.split(":", 1)
        hour = min(23, max(0, int(hour_text)))
        minute = min(59, max(0, int(minute_text)))
        return hour, minute
    except Exception:
        return 14, 50


def time_parts(value: str, fallback: Tuple[int, int]) -> Tuple[int, int]:
    raw = str(value or "").strip()
    try:
        hour_text, minute_text = raw.split(":", 1)
        return min(23, max(0, int(hour_text))), min(59, max(0, int(minute_text)))
    except Exception:
        return fallback


def auto_update_window(now: datetime, start_time: str, until_time: str) -> Tuple[datetime, datetime]:
    start_hour, start_minute = time_parts(start_time, (14, 30))
    end_hour, end_minute = time_parts(until_time, (23, 59))
    return (
        now.replace(hour=start_hour, minute=start_minute, second=0, microsecond=0),
        now.replace(hour=end_hour, minute=end_minute, second=0, microsecond=0),
    )


def within_auto_update_window(now: datetime, start_time: str, until_time: str) -> bool:
    if now.weekday() >= 5:
        return False
    start_at, end_at = auto_update_window(now, start_time, until_time)
    return start_at <= now <= end_at


def next_auto_update_window_start(now: datetime, start_time: str, until_time: str) -> datetime:
    start_at, _ = auto_update_window(now, start_time, until_time)
    if now.weekday() < 5 and now < start_at:
        return start_at
    candidate = now + timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate = candidate + timedelta(days=1)
    start_hour, start_minute = time_parts(start_time, (14, 30))
    return candidate.replace(hour=start_hour, minute=start_minute, second=0, microsecond=0)


def next_auto_snapshot_at(now: datetime, snapshot_time: str) -> datetime:
    hour, minute = auto_snapshot_time_parts(snapshot_time)
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= now:
        candidate = candidate + timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate = candidate + timedelta(days=1)
    return candidate


def after_auto_snapshot_time(now: datetime, snapshot_time: str) -> bool:
    hour, minute = auto_snapshot_time_parts(snapshot_time)
    scheduled_today = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return now >= scheduled_today


def run_validation_tuning_once(
    validation_store,
    cached_metrics: Callable[[str, int], Dict[str, object]],
    deepseek_review_builder: Callable[[str, Dict[str, object], int], Dict[str, object]],
    strategies: List[str],
    days: int = 20,
    use_deepseek: bool = True,
) -> Dict[str, object]:
    result = {
        "ok": True,
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "days": int(days),
        "use_deepseek": bool(use_deepseek),
        "runs": [],
    }
    for strategy in strategies:
        try:
            dates = validation_store.list_signal_dates(strategy)
            metrics = cached_metrics(strategy, days)
            deepseek_review = deepseek_review_builder(strategy, metrics, days) if use_deepseek else {
                "enabled": False,
                "status": "skipped",
            }
            if use_deepseek:
                deepseek_review = _attach_deepseek_oos_evaluations(validation_store, strategy, deepseek_review, days)
            plan = build_strategy_tuning_plan(
                strategy_name=strategy,
                metrics=metrics,
                dates=dates,
                deepseek_review=deepseek_review,
                days=days,
            )
            saved = validation_store.save_tuning_run(strategy, days, plan, metrics, deepseek_review)
            result["runs"].append(
                {
                    "ok": True,
                    "strategy": strategy,
                    "status": plan.get("status"),
                    "can_apply": bool(plan.get("can_apply")),
                    "shadow_mode": bool(plan.get("shadow_mode")),
                    "saved": saved,
                }
            )
        except Exception as exc:
            result["ok"] = False
            result["runs"].append({"ok": False, "strategy": strategy, "error": str(exc)})
    result["finished_at"] = datetime.now().isoformat(timespec="seconds")
    return result


def _attach_deepseek_oos_evaluations(
    validation_store,
    strategy: str,
    deepseek_review: Dict[str, object],
    days: int,
) -> Dict[str, object]:
    if not isinstance(deepseek_review, dict) or not deepseek_review.get("rule_candidates"):
        return deepseek_review
    try:
        from .calibrate import calibrate_blend_alpha, evaluate_deepseek_rule

        samples = validation_store.live_weight_samples(strategy, days=max(60, int(days)))
        top_k = 5 if strategy == "tomorrow_picks" else 10
        evaluations = []
        next_rules = []
        for rule in (deepseek_review.get("rule_candidates") or [])[:4]:
            if not isinstance(rule, dict):
                continue
            evaluation = evaluate_deepseek_rule(strategy, rule, samples, top_k=top_k, dry_run=True)
            enriched_rule = dict(rule)
            enriched_rule["oos_evaluation"] = evaluation
            enriched_rule["can_apply"] = bool(evaluation.get("can_apply"))
            evaluations.append(evaluation)
            next_rules.append(enriched_rule)
        result = dict(deepseek_review)
        result["rule_candidates"] = next_rules
        result["rule_evaluations"] = evaluations
        result["blend_alpha_calibration"] = calibrate_blend_alpha(
            strategy,
            samples,
            top_k=top_k,
            dry_run=True,
            write_alpha_zero=bool(getattr(config, "DEEPSEEK_WRITE_ALPHA_ZERO", True)),
        )
        return result
    except Exception as exc:
        result = dict(deepseek_review)
        result["oos_error"] = str(exc)
        return result


def run_validation_auto_snapshot_once(
    *,
    normalize_market: Callable[[str], str],
    provider,
    validation_store,
    auto_snapshot_lock,
    auto_snapshot_status: Dict[str, object],
    configured_auto_snapshot_strategies_fn: Callable[[], List[str]],
    run_snapshots_fn: Callable,
    invalidate_metrics_cache: Callable[[], None],
    run_validation_tuning_once_fn: Callable[..., Dict[str, object]],
    set_auto_snapshot_status: Callable[..., None],
) -> Dict[str, object]:
    if not config.VALIDATION_AUTO_SNAPSHOT_ENABLED:
        return {"ok": True, "status": "disabled"}
    market = normalize_market(config.VALIDATION_AUTO_SNAPSHOT_MARKET)
    with auto_snapshot_lock:
        if auto_snapshot_status.get("running"):
            return {"ok": True, "status": "already_running"}
        auto_snapshot_status["running"] = True
        auto_snapshot_status["last_started_at"] = datetime.now().isoformat(timespec="seconds")
        auto_snapshot_status["last_error"] = ""

    strategies = configured_auto_snapshot_strategies_fn()
    result = {"ok": True, "strategies": strategies, "market": market, "snapshots": []}
    try:
        snapshot_results = run_snapshots_fn(provider, validation_store, strategies, market=market)
        result["snapshots"] = snapshot_results
        failed = [item for item in snapshot_results if not item.get("ok")]
        result["ok"] = not failed
        if failed:
            raise RuntimeError("; ".join(str(item.get("error") or item.get("strategy")) for item in failed[:3]))
        invalidate_metrics_cache()
        now = datetime.now()
        with auto_snapshot_lock:
            last_tuning_date = str(auto_snapshot_status.get("last_tuning_date") or "")
        if now.weekday() < 5 and after_auto_snapshot_time(now, config.VALIDATION_AUTO_SNAPSHOT_TIME) and last_tuning_date != now.date().isoformat():
            tuning_days = max(
                int(getattr(config, "STRATEGY_DECAY_MIN_REAL_DAYS", 60)),
                int(getattr(config, "STRATEGY_VALIDATION_GATE_WINDOW_DAYS", 120)),
            )
            tuning_result = run_validation_tuning_once_fn(strategies, days=tuning_days, use_deepseek=True)
            result["tuning"] = tuning_result
            set_auto_snapshot_status(
                last_tuning_date=now.date().isoformat(),
                last_tuning_result=tuning_result,
            )
        result["backup"] = backup_validation_db(
            config.VALIDATION_DB_PATH,
            config.VALIDATION_BACKUP_PATH,
            label="auto_snapshot",
        )
        result["finished_at"] = datetime.now().isoformat(timespec="seconds")
        set_auto_snapshot_status(
            running=False,
            last_attempt_date=datetime.now().date().isoformat(),
            deadline_missed=False,
            last_finished_at=result["finished_at"],
            last_result=result,
        )
        return result
    except Exception as exc:
        result.update({"ok": False, "error": str(exc), "finished_at": datetime.now().isoformat(timespec="seconds")})
        set_auto_snapshot_status(
            running=False,
            last_finished_at=result["finished_at"],
            last_error=str(exc),
            last_result=result,
        )
        return result


def run_validation_auto_update_once(
    *,
    auto_update_lock,
    auto_update_status: Dict[str, object],
    set_auto_update_status: Callable[..., None],
    run_validation_outcome_update_once_fn: Callable[[], Dict[str, object]],
    run_oos_reports_once_fn: Callable[[], Dict[str, object]] = None,
) -> Dict[str, object]:
    if not config.VALIDATION_AUTO_UPDATE_ENABLED:
        return {"ok": True, "status": "disabled"}
    with auto_update_lock:
        if auto_update_status.get("running"):
            return {"ok": True, "status": "already_running"}
        auto_update_status["running"] = True
        auto_update_status["last_started_at"] = datetime.now().isoformat(timespec="seconds")
        auto_update_status["last_error"] = ""

    started_at = datetime.now().isoformat(timespec="seconds")
    result = {"ok": True, "started_at": started_at, "mode": "outcome_update", "updates": []}
    try:
        update_result = run_validation_outcome_update_once_fn()
        result.update(update_result)
        result["mode"] = "outcome_update"
        result["summary"] = _outcome_update_summary(result.get("updates") or [])
        if not result.get("ok"):
            raise RuntimeError(str(result.get("error") or result.get("status") or "荐股结果回填失败"))
        if run_oos_reports_once_fn is not None:
            oos_result = run_oos_reports_once_fn()
            result["oos_reports"] = oos_result
            result["oos_summary"] = _oos_report_summary(oos_result.get("reports") or [])
            alert_statuses = [
                item
                for item in result["oos_summary"].get("statuses", [])
                if item.get("oos_status")
                in ("empty", "insufficient_oos_days", "needs_backfill", "gate_blocked", "portfolio_blocked")
            ]
            if alert_statuses:
                result["status"] = "oos_attention_required"
                result["alerts"] = alert_statuses
        result["finished_at"] = datetime.now().isoformat(timespec="seconds")
        set_auto_update_status(
            running=False,
            last_finished_at=result["finished_at"],
            last_result=result,
            last_oos_summary=result.get("oos_summary", {}),
            last_oos_alerts=result.get("alerts", []),
        )
        return result
    except Exception as exc:
        result["ok"] = False
        result["error"] = str(exc)
        result["finished_at"] = datetime.now().isoformat(timespec="seconds")
        set_auto_update_status(
            running=False,
            last_finished_at=result["finished_at"],
            last_error=str(exc),
            last_result=result,
        )
        return result


def _outcome_update_summary(updates: List[Dict[str, object]]) -> Dict[str, object]:
    summary = {
        "requested": 0,
        "updated": 0,
        "pending": 0,
        "unknown": 0,
        "skipped": 0,
        "execution_skipped": 0,
        "skipped_reasons": {},
        "error_count": 0,
    }
    for item in updates or []:
        if item.get("error"):
            summary["error_count"] += 1
            continue
        result = item.get("result") if isinstance(item, dict) else {}
        if not isinstance(result, dict):
            continue
        for key in ("requested", "updated", "pending", "unknown", "skipped", "execution_skipped"):
            summary[key] += int(result.get(key) or 0)
        for reason, count in (result.get("skipped_reasons") or {}).items():
            reason_key = str(reason or "unknown").strip() or "unknown"
            summary["skipped_reasons"][reason_key] = summary["skipped_reasons"].get(reason_key, 0) + int(count or 0)
    return summary


def _oos_report_summary(reports: List[Dict[str, object]]) -> Dict[str, object]:
    summary = {
        "report_count": 0,
        "needs_backfill_count": 0,
        "gate_blocked_count": 0,
        "portfolio_blocked_count": 0,
        "insufficient_oos_days_count": 0,
        "oos_passed_count": 0,
        "empty_count": 0,
        "error_count": 0,
        "statuses": [],
    }
    for item in reports or []:
        strategy = str(item.get("strategy") or item.get("strategy_name") or "")
        if item.get("error"):
            summary["error_count"] += 1
            summary["statuses"].append(
                {"strategy": strategy, "oos_status": "error", "error": str(item.get("error") or "")}
            )
            continue
        report = item.get("report") if isinstance(item.get("report"), dict) else item
        oos_status = str(report.get("oos_status") or item.get("oos_status") or "")
        if oos_status == "needs_backfill":
            summary["needs_backfill_count"] += 1
        elif oos_status == "gate_blocked":
            summary["gate_blocked_count"] += 1
        elif oos_status == "portfolio_blocked":
            summary["portfolio_blocked_count"] += 1
        elif oos_status == "insufficient_oos_days":
            summary["insufficient_oos_days_count"] += 1
        elif oos_status == "oos_passed":
            summary["oos_passed_count"] += 1
        elif oos_status == "empty":
            summary["empty_count"] += 1
        if oos_status:
            summary["report_count"] += 1
            summary["statuses"].append(
                {
                    "strategy": strategy,
                    "oos_status": oos_status,
                    "blockers": report.get("blockers") or [],
                    "readiness": report.get("readiness") or {},
                }
            )
    summary["attention_count"] = (
        summary["empty_count"]
        + summary["insufficient_oos_days_count"]
        + summary["needs_backfill_count"]
        + summary["gate_blocked_count"]
        + summary["portfolio_blocked_count"]
    )
    return summary


def start_validation_auto_update_worker(
    *,
    worker_set,
    worker_lock,
    set_auto_update_status: Callable[..., None],
    within_auto_update_window_fn: Callable[[datetime], bool],
    next_auto_update_window_start_fn: Callable[[datetime], datetime],
    run_validation_auto_update_once_fn: Callable[[], Dict[str, object]],
) -> None:
    if not config.VALIDATION_AUTO_UPDATE_ENABLED:
        return
    worker_key = "{}|{}".format(config.VALIDATION_DB_PATH, config.HISTORY_CACHE_PATH)
    with worker_lock:
        if worker_key in worker_set:
            return
        worker_set.add(worker_key)

    def _worker_loop():
        initial_delay = max(0, int(config.VALIDATION_AUTO_UPDATE_INITIAL_DELAY_SECONDS))
        if initial_delay:
            time.sleep(initial_delay)
        while True:
            interval = max(60, int(config.VALIDATION_AUTO_UPDATE_INTERVAL_SECONDS))
            now = datetime.now()
            if within_auto_update_window_fn(now):
                run_validation_auto_update_once_fn()
                set_auto_update_status(next_run_after_seconds=interval)
                time.sleep(interval)
                continue
            next_run_at = next_auto_update_window_start_fn(now)
            sleep_seconds = max(60, min(3600, int((next_run_at - now).total_seconds())))
            set_auto_update_status(
                next_run_after_seconds=sleep_seconds,
                next_run_at=next_run_at.isoformat(timespec="seconds"),
            )
            time.sleep(sleep_seconds)

    set_auto_update_status(started=True)
    thread = threading.Thread(target=_worker_loop, name="validation-auto-update", daemon=True)
    thread.start()


def start_validation_auto_snapshot_worker(
    *,
    worker_set,
    worker_lock,
    auto_snapshot_lock,
    auto_snapshot_status: Dict[str, object],
    auto_snapshot_time_parts_fn: Callable[[], Tuple[int, int]],
    next_auto_snapshot_at_fn: Callable[[datetime], datetime],
    set_auto_snapshot_status: Callable[..., None],
    run_validation_auto_snapshot_once_fn: Callable[[], Dict[str, object]],
) -> None:
    if not config.VALIDATION_AUTO_SNAPSHOT_ENABLED:
        return
    worker_key = "snapshot|{}|{}|{}|{}".format(
        config.VALIDATION_DB_PATH,
        config.VALIDATION_AUTO_SNAPSHOT_TIME,
        config.VALIDATION_AUTO_SNAPSHOT_MARKET,
        getattr(config, "VALIDATION_AUTO_SNAPSHOT_STRATEGIES", ""),
    )
    with worker_lock:
        if worker_key in worker_set:
            return
        worker_set.add(worker_key)

    def _worker_loop():
        while True:
            now = datetime.now()
            hour, minute = auto_snapshot_time_parts_fn()
            scheduled_today = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            today = now.date().isoformat()
            with auto_snapshot_lock:
                last_attempt_date = auto_snapshot_status.get("last_attempt_date", "")
            if now.weekday() < 5 and now >= scheduled_today and last_attempt_date != today:
                snapshot_result = run_validation_auto_snapshot_once_fn()
                now = datetime.now()
                if not snapshot_result.get("ok"):
                    cutoff_hour, cutoff_minute = time_parts(
                        getattr(config, "TOMORROW_SIGNAL_CUTOFF_TIME", "14:55"),
                        (14, 55),
                    )
                    cutoff_today = now.replace(
                        hour=cutoff_hour,
                        minute=cutoff_minute,
                        second=0,
                        microsecond=0,
                    )
                    if now >= cutoff_today:
                        set_auto_snapshot_status(
                            last_attempt_date=today,
                            deadline_missed=True,
                            deadline_missed_at=now.isoformat(timespec="seconds"),
                        )
                        continue
                    retry_seconds = max(60, int(getattr(config, "VALIDATION_AUTO_SNAPSHOT_RETRY_SECONDS", 60)))
                    next_run_at = now + timedelta(seconds=retry_seconds)
                    set_auto_snapshot_status(next_run_at=next_run_at.isoformat(timespec="seconds"))
                    time.sleep(retry_seconds)
                    continue
            next_run_at = next_auto_snapshot_at_fn(now)
            set_auto_snapshot_status(next_run_at=next_run_at.isoformat(timespec="seconds"))
            sleep_seconds = max(30, min(3600, int((next_run_at - now).total_seconds())))
            time.sleep(sleep_seconds)

    set_auto_snapshot_status(started=True)
    thread = threading.Thread(target=_worker_loop, name="validation-auto-snapshot", daemon=True)
    thread.start()
