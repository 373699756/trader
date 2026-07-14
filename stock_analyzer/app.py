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
from .services.app_services import AppServiceHooks, AppServices


def create_app() -> Flask:
    from . import config

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
    # The quote scheduler is process-local and owns one coalesced refresh loop.
    # Other research/validation jobs remain outside the web process.
    realtime_scheduler = getattr(container, "realtime_scheduler", None)
    if bool(getattr(config, "REALTIME_MARKET_SCHEDULER_ENABLED", True)) and callable(
        getattr(realtime_scheduler, "start", None)
    ):
        realtime_scheduler.start()

    app.extensions["app_container"] = container
    app.extensions["app_services"] = services
    if bool(getattr(config, "DEEPSEEK_INTERNAL_SCHEDULER_ENABLED", True)):
        from .deepseek_scheduler import start_deepseek_scheduler

        start_deepseek_scheduler()
    app.register_blueprint(recommendations_bp)
    app.register_blueprint(prediction_bp)
    app.register_blueprint(validation_bp)
    return app
