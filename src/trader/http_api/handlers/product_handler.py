"""The complete read-only HTTP product surface."""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import date
from functools import partial

from flask import Blueprint, Flask, Response, jsonify, render_template, request

from trader.http_api.freeze_sse import FreezeEventResponse
from trader.http_api.response.freeze_projection import SerializeError, SerializeFreezeView
from trader.http_api.route_services import ProfiledWebServices, UnifiedWebServices, WebServices
from trader.http_api.schemas.status_schema import STATUS_SCHEMA_VERSION
from trader.recommendation.application.pipeline.freeze_publish.event_stream import UnifiedSubscriberLimitError
from trader.recommendation.application.pipeline.freeze_publish.read_only_queries import (
    FREEZE_VIEW_SCHEMA_VERSION,
    FreezeView,
)
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.domain.scoring.profile_identity import SCORING_PROFILE_IDS

RouteResponse = Response | tuple[Response, int]
_DEEPSEEK_ZERO_CALL_REASONS = frozenset(
    {
        "disabled",
        "api_key_missing",
        "no_eligible_candidates",
        "all_candidates_cached",
        "budget_exhausted",
        "bucket_limit",
        "stage_limit",
        "daily_hard_limit",
        "circuit_open",
        "deadline_reached",
        "batch_skipped",
        "no_physical_attempt_recorded",
    }
)


def RegisterRoutes(app: Flask, services: WebServices | None) -> None:
    app.register_blueprint(_create_product_blueprint(services))


def _create_product_blueprint(services: WebServices | None) -> Blueprint:
    blueprint = Blueprint("product", __name__)
    blueprint.add_url_rule("/", "root", partial(_root, services))
    blueprint.add_url_rule(
        "/api/freezes/<strategy_name>/current",
        "freeze_current",
        partial(_current, services),
    )
    blueprint.add_url_rule(
        "/api/freezes/<strategy_name>/history",
        "freeze_history",
        partial(_history, services),
    )
    blueprint.add_url_rule(
        "/api/freezes/<strategy_name>/dates",
        "freeze_dates",
        partial(_dates, services),
    )
    blueprint.add_url_rule("/api/status", "status", partial(_status, services))
    blueprint.add_url_rule("/api/company/<code>/documents", "company_documents", partial(_company_documents, services))
    blueprint.add_url_rule("/api/events", "events", partial(_events, services))
    return blueprint


def _root(services: WebServices | None) -> str:
    retention_seconds = services.config.snapshot_retention_seconds if services is not None else 0.0
    profiles = (
        tuple(services.profiles)
        if isinstance(services, ProfiledWebServices)
        else (services.profile_id,)
        if isinstance(services, UnifiedWebServices)
        else ()
    )
    default_profile = (
        services.default_profile
        if isinstance(services, ProfiledWebServices)
        else services.profile_id
        if isinstance(services, UnifiedWebServices)
        else ""
    )
    long_profile = services.long_profile if isinstance(services, ProfiledWebServices) else default_profile
    return render_template(
        "index.html",
        web_snapshot_retention_ms=round(retention_seconds * 1000),
        scoring_profiles=profiles,
        default_scoring_profile=default_profile,
        long_scoring_profile=long_profile,
    )


def _current(services: WebServices | None, strategy_name: str) -> RouteResponse:
    strategy = _strategy(strategy_name)
    if strategy is None:
        return _error("invalid_strategy", "strategy must be tomorrow, d25 or long", 400)
    invalid_profile = _invalid_profile_response(services)
    if invalid_profile is not None:
        return invalid_profile
    selected = _select_services(services, strategy=strategy)
    if selected is None:
        return _not_ready()
    profile_id, lane = selected
    return _view_response(lane.queries.current(strategy), profile_id=profile_id)


def _history(services: WebServices | None, strategy_name: str) -> RouteResponse:
    strategy = _strategy(strategy_name)
    if strategy is None:
        return _error("invalid_strategy", "strategy must be tomorrow, d25 or long", 400)
    raw_date = request.args.get("date", "")
    trade_date = _strict_date(raw_date)
    if trade_date is None:
        return _error("invalid_date", "date must use YYYY-MM-DD", 400)
    invalid_profile = _invalid_profile_response(services)
    if invalid_profile is not None:
        return invalid_profile
    selected = _select_services(services, strategy=strategy)
    if selected is None:
        return _not_ready()
    profile_id, lane = selected
    return _view_response(lane.queries.history(strategy, trade_date), profile_id=profile_id)


def _dates(services: WebServices | None, strategy_name: str) -> RouteResponse:
    strategy = _strategy(strategy_name)
    if strategy is None:
        return _error("invalid_strategy", "strategy must be tomorrow, d25 or long", 400)
    invalid_profile = _invalid_profile_response(services)
    if invalid_profile is not None:
        return invalid_profile
    selected = _select_services(services, strategy=strategy)
    if selected is None:
        return _not_ready()
    profile_id, lane = selected
    return jsonify(
        {
            "schema_version": "freeze_dates",
            "strategy": strategy.value,
            "scoring_profile": profile_id,
            "dates": [value.isoformat() for value in lane.queries.dates(strategy)],
        }
    )


def _company_documents(services: WebServices | None, code: str) -> RouteResponse:
    invalid_profile = _invalid_profile_response(services)
    if invalid_profile is not None:
        return invalid_profile
    selected = _select_services(services)
    if selected is None:
        return _not_ready()
    try:
        documents = selected[1].queries.company_documents(code, tuple(request.args.getlist("evidence_id")))
    except ValueError:
        return _error("invalid_company_evidence_query", "code and 1–32 evidence IDs are required", 400)
    except (OSError, RuntimeError):
        return _error("company_evidence_unavailable", "company evidence is temporarily unavailable", 503)
    return jsonify(
        {
            "schema_version": "company_documents",
            "code": code,
            "documents": [
                {
                    "evidence_id": item.evidence_id,
                    "title": item.title,
                    "url": item.url,
                    "published_at": item.disclosed_at.isoformat(),
                    "body_checked": item.text_status == "extracted",
                }
                for item in documents
            ],
        }
    )


def _status(services: WebServices | None) -> RouteResponse:
    invalid_profile = _invalid_profile_response(services)
    if invalid_profile is not None:
        return invalid_profile
    selected = _select_services(services)
    if selected is None:
        return _not_ready()
    profile_id, lane = selected
    try:
        runtime = lane.status_provider()
    except (OSError, RuntimeError, TypeError, ValueError):
        runtime = {"status": "degraded", "degraded_reasons": ("runtime_status_unavailable",)}
    stream = lane.events.status()
    strategies = {
        strategy.value: _strategy_status(_services_for_strategy(services, lane, strategy).queries.current(strategy))
        for strategy in Strategy
    }
    return jsonify(
        {
            "schema_version": STATUS_SCHEMA_VERSION,
            "release": {
                "freeze_view_schema": FREEZE_VIEW_SCHEMA_VERSION,
            },
            "status": str(runtime.get("status", "running")),
            "phase": runtime.get("phase"),
            **_preview_status(runtime),
            "runtime_started": bool(runtime.get("runtime_started", True)),
            "runtime_version": runtime.get("runtime_version"),
            "company_research": _company_research(runtime),
            "scheduler": _mapping(runtime.get("scheduler")),
            "market_data": _market_data(runtime),
            "scoring_profile": _scoring_profile(runtime),
            "selected_profile": profile_id,
            "available_profiles": list(services.profiles)
            if isinstance(services, ProfiledWebServices)
            else [profile_id],
            "last_error": runtime.get("last_error"),
            "deepseek_budget": _budget(runtime),
            "deepseek": _deepseek(runtime),
            "degraded_reasons": list(_reasons(runtime)),
            "health": _health(runtime),
            "recent_errors": _recent_errors(runtime),
            "strategies": strategies,
            "events": {
                "sequence": stream.sequence,
                "history_size": stream.history_size,
                "subscriber_count": stream.subscriber_count,
                "slow_subscriber_drops": stream.slow_subscriber_drops,
            },
        }
    )


def _preview_status(runtime: Mapping[str, object]) -> dict[str, object]:
    if runtime.get("run_mode") != "preview":
        return {}
    return {
        "run_mode": "preview",
        **{
            name: _optional_string(runtime.get(name))
            for name in (
                "preview_run_id",
                "market_trade_date",
                "market_as_of",
                "preview_started_at",
                "preview_state",
                "preview_error",
            )
        },
    }


def _events(services: WebServices | None) -> RouteResponse:
    invalid_profile = _invalid_profile_response(services)
    if invalid_profile is not None:
        return invalid_profile
    selected = _select_services(services)
    if selected is None:
        return _not_ready()
    _, lane = selected
    raw_cursor = request.headers.get("Last-Event-ID")
    if raw_cursor is None:
        raw_cursor = request.args.get("cursor")
    cursor = _cursor(raw_cursor)
    if raw_cursor is not None and cursor is None:
        return _error("invalid_cursor", "event cursor must be a non-negative integer", 400)
    try:
        subscription = lane.events.open_subscription(cursor)
    except UnifiedSubscriberLimitError:
        return _error("stream_capacity", "event stream connection limit reached", 503)
    return FreezeEventResponse(
        lane.events,
        subscription,
        heartbeat_seconds=lane.config.heartbeat_seconds,
    )


def _view_response(view: FreezeView, *, profile_id: str | None = None) -> Response:
    if view.etag is not None and request.if_none_match.contains(view.etag):
        response = Response(status=304)
        response.set_etag(view.etag)
        return response
    payload = SerializeFreezeView(view)
    if profile_id is not None:
        payload["scoring_profile"] = profile_id
    response = jsonify(payload)
    if view.etag is not None:
        response.set_etag(view.etag)
    response.headers["Cache-Control"] = "no-cache"
    return response


def _select_services(
    services: WebServices | None,
    *,
    strategy: Strategy | None = None,
) -> tuple[str, UnifiedWebServices] | None:
    if services is None:
        return None
    if isinstance(services, ProfiledWebServices):
        if strategy is Strategy.LONG:
            return services.select_long()
        return services.select(request.args.get("profile"))
    return services.profile_id, services


def _services_for_strategy(
    services: WebServices | None,
    selected: UnifiedWebServices,
    strategy: Strategy,
) -> UnifiedWebServices:
    if strategy is Strategy.LONG and isinstance(services, ProfiledWebServices):
        return services.select_long()[1]
    return selected


def _invalid_profile_response(services: WebServices | None) -> RouteResponse | None:
    if not isinstance(services, ProfiledWebServices):
        return None
    profile = request.args.get("profile")
    if profile is None or profile in services.profiles:
        return None
    return _error("invalid_profile", f"profile must be one of {', '.join(services.profiles)}", 400)


def _strategy_status(view: FreezeView) -> dict[str, object]:
    return {
        "status": view.status,
        "trade_date": view.trade_date.isoformat() if view.trade_date is not None else None,
        "freeze_version": view.freeze_version,
        "projection_version": view.projection_version if view.freeze_version is not None else None,
        "data_age_seconds": view.data_age_seconds,
        "stage": view.stage,
        "score_status": view.score_status,
        "frozen": view.frozen,
        "coverage": {
            "candidate_count": view.coverage.candidate_count,
            "evaluated_count": view.coverage.evaluated_count,
            "rejected_count": view.coverage.rejected_count,
            "selected_count": view.coverage.selected_count,
        },
        "degraded_reasons": list(view.degraded_reasons),
    }


def _budget(runtime: Mapping[str, object]) -> Mapping[str, object]:
    direct = runtime.get("deepseek_budget")
    if isinstance(direct, Mapping):
        return _budget_mapping(direct)
    dependencies = runtime.get("dependencies")
    if isinstance(dependencies, Mapping):
        deepseek = dependencies.get("deepseek")
        if isinstance(deepseek, Mapping) and isinstance(deepseek.get("budget"), Mapping):
            budget = deepseek["budget"]
            if isinstance(budget, Mapping):
                return _budget_mapping(budget)
    return {"available": False, "error": "budget_status_unavailable"}


def _budget_mapping(raw: Mapping[object, object]) -> dict[str, object]:
    budget = _json_mapping(raw)
    if _non_negative_number(budget.get("limit")) is None:
        used = _non_negative_number(budget.get("used"))
        remaining = _non_negative_number(budget.get("remaining"))
        if used is not None and remaining is not None:
            total = used + remaining
            budget["limit"] = int(total) if total.is_integer() else total
    return budget


def _reference_publication(raw: object) -> dict[str, object]:
    if not isinstance(raw, Mapping):
        return {}
    return {
        "reference_publication": {
            field: raw[field]
            for field in ("loaded_identity", "disk_identity", "restart_required", "failure")
            if field in raw and _json_scalar(raw[field])
        }
    }


def _market_data(runtime: Mapping[str, object]) -> dict[str, object]:
    raw = runtime.get("market_data")
    if not isinstance(raw, Mapping):
        return {}
    scalar_fields = (
        "status",
        "latest_source",
        "market_epoch",
        "reference_epoch",
        "market_feature_rows",
        "candidate_quote_cache_entries",
        "candidate_quote_latest_source",
        "history_universe_rows",
        "history_covered_rows",
        "history_coverage_ratio",
        "history_error_count",
        "published_history_state",
        "published_history_snapshot_hash",
        "published_history_disk_snapshot_hash",
        "outcome_history_snapshot_hash",
        "outcome_history_disk_snapshot_hash",
        "restart_required",
        "published_history_data_cutoff",
        "history_maintenance_state",
        "history_maintenance_reason",
        "history_maintenance_stage",
        "history_maintenance_completed_units",
        "history_maintenance_total_units",
        "measured_at",
    )
    result = {field: raw[field] for field in scalar_fields if field in raw and _json_scalar(raw[field])}
    result.update(_issuer_eligibility(raw.get("issuer_eligibility")))
    result.update(_reference_publication(raw.get("reference_publication")))
    for field in ("market_quote_age", "candidate_quote_age"):
        value = raw.get(field)
        if isinstance(value, Mapping):
            result[field] = {
                key: value[key]
                for key in (
                    "sample_count",
                    "p50_seconds",
                    "p95_seconds",
                    "maximum_seconds",
                    "latest_source_time",
                )
                if key in value and _json_scalar(value[key])
            }
    security_master = raw.get("security_master")
    if isinstance(security_master, Mapping):
        result["security_master"] = {
            key: security_master[key]
            for key in (
                "total_rows",
                "listing_date_rows",
                "listing_age_rows",
                "complete_rows",
                "provider",
                "tushare_required",
                "persistence_schedule_error_count",
            )
            if key in security_master and _json_scalar(security_master[key])
        }
    sources = raw.get("sources")
    if isinstance(sources, Mapping):
        safe_sources: dict[str, object] = {}
        source_fields = (
            "planned_count",
            "success_count",
            "error_count",
            "timeout_count",
            "circuit_open",
            "last_latency_ms",
            "p50_latency_ms",
            "p95_latency_ms",
            "data_age_seconds",
            "enabled",
            "access_points",
            "history_mode",
            "minute_call_limit",
            "daily_call_limit",
            "process_api_attempts_last_minute",
            "process_api_attempts_today",
            "process_remaining_calls_today",
            "local_rate_limit_count",
            "snapshot_rows",
            "invalid_rows",
            "listing_date_rows",
            "last_error_code",
            "timeout_seconds",
            "loaded_identity",
            "disk_identity",
            "restart_required",
        )
        for name, value in sorted(sources.items(), key=lambda item: str(item[0]))[:8]:
            if not isinstance(value, Mapping):
                continue
            safe_sources[str(name)[:32]] = {
                field: value[field] for field in source_fields if field in value and _json_scalar(value[field])
            }
        result["sources"] = safe_sources
    if changes := _market_changes(raw.get("market_changes")):
        result["market_changes"] = changes
    canonical_snapshot = raw.get("canonical_snapshot")
    if isinstance(canonical_snapshot, Mapping):
        projected_snapshot = {
            key: canonical_snapshot[key]
            for key in (
                "observed_at",
                "merge_epoch",
                "market_epoch",
                "reference_epoch",
                "conflicts",
                "degraded_reasons",
                "failure_categories",
                "status",
            )
            if key in canonical_snapshot
            and (_json_scalar(canonical_snapshot[key]) or isinstance(canonical_snapshot[key], (tuple, list)))
        }
        ages = canonical_snapshot.get("source_ages_seconds")
        if isinstance(ages, Mapping):
            projected_snapshot["source_ages_seconds"] = {
                str(source): value for source, value in ages.items() if _json_scalar(value)
            }
        if projected_snapshot:
            result["canonical_snapshot"] = projected_snapshot
    if waterfall := _latency_waterfall(raw.get("latency_waterfall")):
        result["latency_waterfall"] = waterfall
    return result


def _company_research(runtime: Mapping[str, object]) -> dict[str, object]:
    raw = runtime.get("company_research")
    if not isinstance(raw, Mapping):
        return {}
    scalar_fields = (
        "state",
        "trade_date",
        "tracked_strategies",
        "tracked_output_codes",
    )
    result = {field: raw[field] for field in scalar_fields if field in raw and _json_scalar(raw[field])}
    return result


def _issuer_eligibility(raw: object) -> dict[str, object]:
    if not isinstance(raw, Mapping):
        return {}
    eligibility = {
        key: raw[key]
        for key in (
            "schema_version",
            "fact_count",
            "excluded_count",
            "manifest_hash",
            "integrity_ok",
            "persistence_error_count",
            "last_error",
        )
        if key in raw and _json_scalar(raw[key])
    }
    reason_counts = raw.get("reason_counts")
    if isinstance(reason_counts, Mapping):
        eligibility["reason_counts"] = {
            str(reason)[:64]: count
            for reason, count in sorted(reason_counts.items(), key=lambda item: str(item[0]))[:32]
            if isinstance(count, int) and not isinstance(count, bool) and count >= 0
        }
    return {"issuer_eligibility": eligibility}


def _scoring_profile(runtime: Mapping[str, object]) -> dict[str, object]:
    raw = runtime.get("scoring_profile")
    if not isinstance(raw, Mapping):
        return {"profile_id": None, "heads": {}}
    profile_id = raw.get("profile_id")
    heads = raw.get("heads")
    if not isinstance(profile_id, str) or not isinstance(heads, Mapping):
        return {"profile_id": None, "heads": {}}
    return {
        "profile_id": profile_id,
        "maintenance": _training_maintenance(raw.get("maintenance")),
        "heads": {
            strategy: _scoring_head(head)
            for strategy, head in sorted(heads.items(), key=lambda item: str(item[0]))
            if strategy in {"tomorrow", "d25"} and isinstance(head, Mapping)
        },
    }


def _training_maintenance(raw: object) -> list[dict[str, object]]:
    if not isinstance(raw, (list, tuple)):
        return []
    result: list[dict[str, object]] = []
    for row in raw[: len(SCORING_PROFILE_IDS) * 2]:
        if not isinstance(row, Mapping):
            continue
        item = {
            key: row[key]
            for key in (
                "profile_id",
                "strategy",
                "owned",
                "restart_required",
                "training_due",
                "training_due_reason",
                "current_label_cutoff",
                "matured_label_days_since_training",
                "failure",
            )
            if key in row and _json_scalar(row[key])
        }
        for key in ("loaded", "disk"):
            identity = row.get(key)
            item[key] = (
                {
                    field: identity[field]
                    for field in (
                        "model_hash",
                        "publication_hash",
                        "label_cutoff",
                        "training_history_hash",
                        "training_run_hash",
                    )
                    if field in identity and _json_scalar(identity[field])
                }
                if isinstance(identity, Mapping)
                else None
            )
        result.append(item)
    return result


def _scoring_head(raw: Mapping[object, object]) -> dict[str, object]:
    result = {
        field: raw[field]
        for field in (
            "is_enable",
            "status",
            "profile_id",
            "model_id",
            "model_hash",
            "scoring_version",
            "activation_basis",
            "historical_status",
            "monitoring_mode",
            "automatic_model_update",
            "loss_probability_status",
            "training_anchor",
            "runtime_anchor",
            "point_in_time_parity",
            "score_scale",
            "production_authority",
            "training_label_cutoff",
        )
        if field in raw and _json_scalar(raw[field])
    }
    failures = raw.get("historical_failure_reasons")
    if isinstance(failures, (tuple, list)):
        result["historical_failure_reasons"] = [str(item) for item in failures if isinstance(item, str)]
    computation = raw.get("computation")
    if isinstance(computation, Mapping):
        result["computation"] = _model_computation(computation)
    return result


def _model_computation(raw: Mapping[str, object]) -> dict[str, object]:
    result: dict[str, object] = {
        field: raw[field]
        for field in (
            "candidate_count",
            "request_count",
            "cache_hit_count",
            "predictor_batch_count",
            "freeze_age_ms",
            "deadline_abandon_reason",
        )
        if field in raw and (_json_scalar(raw[field]) or raw[field] is None)
    }
    for field in ("computed_groups", "reused_groups"):
        value = raw.get(field)
        if isinstance(value, (tuple, list)):
            result[field] = [str(item) for item in value if isinstance(item, str)]
    durations = raw.get("stage_durations")
    result["stage_durations"] = (
        [
            {
                field: item[field]
                for field in (
                    "calculator_group",
                    "execution_count",
                    "last_duration_ms",
                    "cumulative_duration_ms",
                )
                if field in item and _json_scalar(item[field])
            }
            for item in durations
            if isinstance(item, Mapping)
        ]
        if isinstance(durations, (tuple, list))
        else []
    )
    return result


def _market_changes(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        return {}
    return {
        field: value[field]
        for field in ("merge_epoch", "inserted", "updated", "removed", "dirty")
        if field in value and _json_scalar(value[field])
    }


def _latency_waterfall(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        return {}
    fields = (
        "sample_capacity",
        "trace_capacity",
        "stage_capacity",
        "pending_trace_count",
        "planned_count",
        "completed_count",
        "failed_count",
        "timeout_count",
        "superseded_count",
        "dropped_count",
        "dropped_stage_count",
    )
    result = {field: value[field] for field in fields if field in value and _json_scalar(value[field])}
    stages = value.get("stages")
    if isinstance(stages, Mapping):
        result["stages"] = {
            str(name)[:64]: {
                field: stage[field]
                for field in ("sample_count", "p50_ms", "p95_ms", "maximum_ms")
                if field in stage and _json_scalar(stage[field])
            }
            for name, stage in sorted(stages.items(), key=lambda item: str(item[0]))[:64]
            if isinstance(stage, Mapping)
        }
    return result


def _json_scalar(value: object) -> bool:
    return value is None or isinstance(value, (str, bool, int)) or isinstance(value, float) and math.isfinite(value)


def _non_negative_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    numeric = float(value)
    return numeric if math.isfinite(numeric) and numeric >= 0.0 else None


def _deepseek(runtime: Mapping[str, object]) -> dict[str, object]:
    raw = runtime.get("deepseek")
    if not isinstance(raw, Mapping):
        dependencies = runtime.get("dependencies")
        raw = dependencies.get("deepseek") if isinstance(dependencies, Mapping) else None
    status = raw if isinstance(raw, Mapping) else {}
    enabled = status.get("enabled") is True
    configured = status.get("configured") is True
    attempts = status.get("last_physical_attempts")
    physical_attempts = (
        attempts if isinstance(attempts, int) and not isinstance(attempts, bool) and attempts >= 0 else 0
    )
    acceptance = status.get("physical_call_acceptance")
    raw_reason = acceptance.get("zero_call_reason") if isinstance(acceptance, Mapping) else None
    reason = raw_reason if raw_reason in _DEEPSEEK_ZERO_CALL_REASONS else ""
    if physical_attempts == 0 and not reason:
        reason = "disabled" if not enabled else "api_key_missing" if not configured else "no_physical_attempt_recorded"
    return {
        "enabled": enabled,
        "configured": configured,
        "physical_attempts": physical_attempts,
        "zero_call_reason": reason,
    }


def _reasons(runtime: Mapping[str, object]) -> tuple[str, ...]:
    reasons = runtime.get("degraded_reasons", ())
    if isinstance(reasons, (list, tuple)):
        return tuple(str(reason) for reason in reasons)
    return ()


def _health(runtime: Mapping[str, object]) -> dict[str, object]:
    direct = runtime.get("health")
    recent = _recent_errors(runtime)
    unresolved_count = sum(1 for issue in recent if issue["recovery_status"] == "active")
    level = ""
    issue_count = unresolved_count
    if isinstance(direct, Mapping):
        raw_level = direct.get("level")
        if raw_level in {"normal", "degraded", "error"}:
            level = str(raw_level)
        raw_count = direct.get("issue_count")
        if isinstance(raw_count, int) and not isinstance(raw_count, bool) and raw_count >= 0:
            issue_count = min(raw_count, 20)
    if not level:
        level = "error" if runtime.get("runtime_started") is False else "degraded" if _reasons(runtime) else "normal"
    return {"level": level, "issue_count": issue_count}


def _recent_errors(runtime: Mapping[str, object]) -> list[dict[str, object]]:
    raw = runtime.get("recent_errors")
    if not isinstance(raw, (list, tuple)):
        return []
    result: list[dict[str, object]] = []
    for item in raw[:20]:
        if not isinstance(item, Mapping):
            continue
        code = _optional_string(item.get("code"))
        stage = _optional_string(item.get("stage"))
        if code is None or stage is None:
            continue
        severity = item.get("severity") if item.get("severity") in {"degraded", "error"} else "degraded"
        recovery = item.get("recovery_status") if item.get("recovery_status") in {"active", "recovered"} else "active"
        count = item.get("count")
        result.append(
            {
                "code": code,
                "severity": severity,
                "strategy": _optional_string(item.get("strategy")),
                "stage": stage,
                "occurred_at": _optional_string(item.get("occurred_at")),
                "last_occurred_at": _optional_string(item.get("last_occurred_at")),
                "count": count if isinstance(count, int) and not isinstance(count, bool) and count > 0 else 1,
                "recovery_status": recovery,
                "resolved_at": _optional_string(item.get("resolved_at")),
            }
        )
    return result


def _optional_string(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped[:128] if stripped else None


def _mapping(value: object) -> dict[str, object]:
    return _json_mapping(value) if isinstance(value, Mapping) else {}


def _json_mapping(value: Mapping[object, object]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, item in value.items():
        if isinstance(item, Mapping):
            result[str(key)] = _json_mapping(item)
        elif isinstance(item, (list, tuple)):
            result[str(key)] = [_json_mapping(nested) if isinstance(nested, Mapping) else nested for nested in item]
        else:
            result[str(key)] = item
    return result


def _strategy(raw: str) -> Strategy | None:
    try:
        return Strategy(raw)
    except ValueError:
        return None


def _strict_date(raw: str) -> date | None:
    if len(raw) != 10:
        return None
    try:
        parsed = date.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.isoformat() == raw else None


def _cursor(raw: str | None) -> int | None:
    if raw is None:
        return None
    if not raw.isdigit():
        return None
    value = int(raw)
    return value if value <= 9_223_372_036_854_775_807 else None


def _not_ready() -> tuple[Response, int]:
    return _error("freeze_not_ready", "Read services are not ready", 503)


def _error(code: str, message: str, status_code: int) -> tuple[Response, int]:
    return jsonify(SerializeError(code, message)), status_code


__all__ = ["RegisterRoutes"]
