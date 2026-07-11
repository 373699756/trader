from __future__ import annotations

import threading

from flask import Flask

from .app_container import ApplicationContainer
from .app_runtime_support import deepseek_stock_prediction_review
from .app_support import apply_tomorrow_validation_gate, attach_alphalite_factors, load_local_history_frames
from .backtest import run_alphalite_backtest, run_rolling_alphalite_backtest
from .daily_data import list_market_data_codes
from .deepseek_client import review_strategy_validation
from .providers import MarketDataProvider, TimedCache
from .recommendation_snapshot import save_recommendation_snapshot
from .routes.prediction import bp as prediction_bp
from .routes.recommendations import bp as recommendations_bp
from .routes.validation import bp as validation_bp
from .services.app_services import AppServiceHooks, AppServices


# Compatibility aliases kept for existing tests and local imports.
_attach_alphalite_factors = attach_alphalite_factors
_apply_tomorrow_validation_gate = apply_tomorrow_validation_gate


def create_app() -> Flask:
    app = Flask(__name__, template_folder="../templates", static_folder="../static")
    app.config["TEMPLATES_AUTO_RELOAD"] = True
    app.jinja_env.auto_reload = True

    container = ApplicationContainer()
    services = AppServices(
        container,
        AppServiceHooks(
            deepseek_stock_prediction_review=deepseek_stock_prediction_review,
            list_market_data_codes=list_market_data_codes,
            load_local_history_frames=load_local_history_frames,
            run_alphalite_backtest=run_alphalite_backtest,
            run_rolling_alphalite_backtest=run_rolling_alphalite_backtest,
        ),
    )
    services.start_background_workers()

    app.extensions["app_container"] = container
    app.extensions["app_services"] = services
    app.register_blueprint(recommendations_bp)
    app.register_blueprint(prediction_bp)
    app.register_blueprint(validation_bp)
    return app
