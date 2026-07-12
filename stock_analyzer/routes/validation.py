from __future__ import annotations

from flask import Blueprint, request

from .. import config
from ..app_support import validation_gate_window_days
from ..services.app_services import (
    normalize_market,
    normalize_optional_validation_strategy,
    normalize_validation_strategy,
)
from .common import bool_arg, int_arg, json_result, services


bp = Blueprint("validation", __name__)


def _validation_strategy(default: str = "short_term") -> str:
    return normalize_validation_strategy(request.args.get("strategy", default), default=default)


def _direction_focus_arg():
    raw = request.args.get("direction_focus")
    if raw is None:
        return None
    return raw.lower() in ("1", "true", "yes", "on")


@bp.route("/api/strategy-validation/snapshot", methods=["POST"])
def strategy_snapshot():
    return json_result(
        services().strategy_snapshot(
            request.args.get("strategy", "short_term"),
            normalize_market(request.args.get("market", "all")),
        )
    )


@bp.route("/api/strategy-validation/update", methods=["POST"])
def strategy_validation_update():
    return json_result(
        services().strategy_validation_update(
            request.args.get("date", ""),
            _validation_strategy(),
        )
    )


@bp.route("/api/strategy-validation/auto-update-status")
def strategy_validation_auto_update_status():
    return json_result(services().strategy_validation_auto_update_status())


@bp.route("/api/strategy-validation/prefetch-history", methods=["POST"])
def strategy_validation_prefetch_history():
    return json_result(
        services().strategy_validation_prefetch_history(
            signal_date=request.args.get("date", ""),
            strategy=_validation_strategy(),
            days=int_arg("days", 180, minimum=30, maximum=500),
            limit=int_arg("limit", 500, minimum=1, maximum=2000),
            force=bool_arg("force", False),
            update=request.args.get("update", "1").lower() not in ("0", "false", "no"),
        )
    )


@bp.route("/api/strategy-validation/backfill-current-baseline", methods=["POST"])
def strategy_validation_backfill_current_baseline():
    return json_result(
        services().strategy_validation_backfill_current_baseline(
            strategy=_validation_strategy(),
            days=int_arg("days", 120, minimum=20, maximum=500),
            history_days=int_arg(
                "history_days",
                int(getattr(config, "VALIDATION_AUTO_UPDATE_HISTORY_DAYS", 220)),
                minimum=30,
                maximum=600,
            ),
            limit=int_arg(
                "limit",
                int(getattr(config, "VALIDATION_AUTO_UPDATE_MAX_CODES_PER_RUN", 160)),
                minimum=1,
                maximum=2000,
            ),
            force=bool_arg("force", False),
            execute=bool_arg("execute", False),
        )
    )


@bp.route("/api/strategy-validation/oos-report")
def strategy_validation_oos_report():
    return json_result(
        services().strategy_validation_oos_report(
            _validation_strategy(),
            int_arg("days", validation_gate_window_days(), minimum=20, maximum=500),
        )
    )


@bp.route("/api/strategy-validation/oos-report/history")
def strategy_validation_oos_report_history():
    return json_result(
        services().strategy_validation_oos_report_history(
            normalize_optional_validation_strategy(request.args.get("strategy", "")),
            int_arg("limit", 50, minimum=1, maximum=200),
        )
    )


@bp.route("/api/strategy-validation/readiness")
def strategy_validation_readiness():
    return json_result(services().strategy_validation_readiness())


@bp.route("/api/strategy-validation/portfolio-baseline", methods=["GET", "POST"])
def strategy_validation_portfolio_baseline():
    return json_result(
        services().strategy_validation_portfolio_baseline(
            strategy=_validation_strategy("tomorrow_picks"),
            days=int_arg("days", validation_gate_window_days(), minimum=1, maximum=500),
            signal_date=request.args.get("date", ""),
            ranking_field=request.args.get("ranking_field", "score"),
            model_id=request.args.get("model_id", ""),
            execute=request.method == "POST",
            include_audit=bool_arg("audit", False),
        )
    )


@bp.route("/api/strategy-validation/backfill-samples", methods=["POST"])
def strategy_validation_backfill_samples():
    return json_result(
        services().strategy_validation_backfill_samples(
            strategy=_validation_strategy(),
            days=int_arg("days", 260, minimum=80, maximum=600),
            replay_days=int_arg("replay_days", 20, minimum=1, maximum=80),
            top_n=int_arg("top_n", 30, minimum=1, maximum=50),
            holding_days=int_arg("holding_days", 3, minimum=1, maximum=20),
            limit=int_arg("limit", 120, minimum=10, maximum=500),
            force=bool_arg("force", False),
        )
    )


@bp.route("/api/strategy-validation")
def strategy_validation():
    return json_result(
        services().strategy_validation(
            strategy=_validation_strategy(),
            days=int_arg("days", 20, minimum=1, maximum=120),
            light=bool_arg("light", False),
        )
    )


@bp.route("/api/strategy-validation/runtime-config")
def strategy_validation_runtime_config():
    return json_result(
        services().strategy_validation_runtime_config(
            _validation_strategy(),
            int_arg("days", 120, minimum=20, maximum=500),
        )
    )


@bp.route("/api/strategy-validation/tuning", methods=["GET", "POST"])
def strategy_validation_tuning():
    return json_result(
        services().strategy_validation_tuning(
            strategy=_validation_strategy(),
            days=int_arg("days", 20, minimum=1, maximum=120),
            method=request.method,
            use_deepseek=request.args.get("deepseek", "1").lower() not in ("0", "false", "no", "off"),
        )
    )


@bp.route("/api/strategy-validation/daily")
def strategy_validation_daily():
    return json_result(
        services().strategy_validation_daily(
            signal_date=request.args.get("date", ""),
            strategy=_validation_strategy(),
            should_update=bool_arg("update", False),
            include_quotes=bool_arg("quotes", False),
        )
    )


@bp.route("/api/tomorrow-iteration")
def tomorrow_iteration():
    return json_result(
        services().tomorrow_iteration(
            days=int_arg("days", 120, minimum=30, maximum=240),
            force=bool_arg("force", False),
            direction_focus=_direction_focus_arg(),
        )
    )


@bp.route("/api/tomorrow-iteration/apply", methods=["POST"])
def tomorrow_iteration_apply():
    return json_result(
        services().tomorrow_iteration_apply(
            days=int_arg("days", 120, minimum=30, maximum=240),
            direction_focus=_direction_focus_arg(),
        )
    )


@bp.route("/api/backtest")
def backtest():
    return json_result(
        services().backtest_payload(
            raw_codes=request.args.get("codes", ""),
            top_k=int_arg("top_k", 10, minimum=1, maximum=30),
            holding_days=int_arg("holding_days", 3, minimum=1, maximum=20),
            lookback_days=int_arg("lookback_days", 30, minimum=20, maximum=120),
            rebalance_step=int_arg("rebalance_step", 1, minimum=1, maximum=20),
            mode=request.args.get("mode", "rolling"),
        )
    )
