from __future__ import annotations

from flask import Flask

from .app_container import ApplicationContainer
from .app_support import attach_alphalite_factors, load_local_history_frames
from .backtest import run_alphalite_backtest, run_rolling_alphalite_backtest
from .daily_data import list_market_data_codes
from .providers import MarketDataProvider, TimedCache
from .recommendation_snapshot import save_recommendation_snapshot
from .routes.prediction import bp as prediction_bp
from .routes.recommendations import bp as recommendations_bp
from .routes.validation import bp as validation_bp
from .runtime import RuntimeSupervisor
from .services.app_services import AppServiceHooks, AppServices


def create_app(*, start_runtime: bool = False) -> Flask:
    app = Flask(__name__, template_folder="../templates", static_folder="../static")
    app.config["TEMPLATES_AUTO_RELOAD"] = True
    app.jinja_env.auto_reload = True

    container = ApplicationContainer()
    services = AppServices(
        container,
        AppServiceHooks(
            list_market_data_codes=list_market_data_codes,
            load_local_history_frames=load_local_history_frames,
            run_alphalite_backtest=run_alphalite_backtest,
            run_rolling_alphalite_backtest=run_rolling_alphalite_backtest,
        ),
    )
    runtime_supervisor = RuntimeSupervisor(
        getattr(container, "realtime_scheduler", None),
        start_validation_workers=services.start_validation_workers,
        stop_validation_workers=services.stop_validation_workers,
        stop_transient_workers=services.stop_transient_workers,
    )
    app.extensions["app_container"] = container
    app.extensions["app_services"] = services
    app.extensions["runtime_supervisor"] = runtime_supervisor
    app.register_blueprint(recommendations_bp)
    app.register_blueprint(prediction_bp)
    app.register_blueprint(validation_bp)
    if start_runtime:
        runtime_supervisor.start()
    return app
