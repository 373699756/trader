from unittest.mock import Mock, patch

import pytest

from stock_analyzer import config
from stock_analyzer.app import create_app
from stock_analyzer.app_container import RealtimeMarketScheduler
from stock_analyzer.runtime import RuntimeSupervisor


class _Scheduler:
    def __init__(self):
        self.start_calls = 0
        self.stop_calls = []

    def start(self):
        self.start_calls += 1
        return True

    def stop(self, timeout_seconds=5.0):
        self.stop_calls.append(timeout_seconds)


def test_app_factory_is_runtime_side_effect_free_by_default():
    with patch("stock_analyzer.runtime.RuntimeSupervisor.start") as start:
        app = create_app()

    start.assert_not_called()
    assert "runtime_supervisor" in app.extensions


def test_app_factory_wires_validation_workers_into_runtime_lifecycle():
    with (
        patch.object(config, "REALTIME_MARKET_SCHEDULER_ENABLED", False),
        patch.object(config, "DEEPSEEK_INTERNAL_SCHEDULER_ENABLED", False),
        patch.object(config, "WEB_BACKGROUND_WORKERS_ENABLED", True),
        patch("stock_analyzer.app.AppServices.start_validation_workers", return_value=True) as start_validation,
        patch("stock_analyzer.app.AppServices.stop_validation_workers") as stop_validation,
        patch("stock_analyzer.app.AppServices.stop_transient_workers") as stop_transient,
    ):
        app = create_app(start_runtime=True)
        app.extensions["runtime_supervisor"].stop(timeout_seconds=0.25)

    start_validation.assert_called_once_with()
    stop_validation.assert_called_once_with(0.25)
    stop_transient.assert_called_once_with(0.25)


def test_app_services_stop_owned_transient_workers():
    app = create_app()
    services = app.extensions["app_services"]

    with (
        patch.object(services.context.recommendation_refresh, "stop") as stop_recommendation_refresh,
        patch.object(services.provider, "stop_realtime_quotes") as stop_realtime_quotes,
        patch.object(services.container.snapshot_writer, "stop") as stop_snapshot_writer,
    ):
        services.stop_transient_workers(timeout_seconds=0.25)

    stop_recommendation_refresh.assert_called_once_with(0.25)
    stop_realtime_quotes.assert_called_once_with(0.25)
    stop_snapshot_writer.assert_called_once_with(0.25)


def test_runtime_supervisor_owns_and_stops_started_components():
    realtime = _Scheduler()
    start_deepseek = Mock(return_value=True)
    stop_deepseek = Mock()
    start_validation_workers = Mock(return_value=True)
    stop_validation_workers = Mock()
    stop_transient_workers = Mock()
    supervisor = RuntimeSupervisor(
        realtime,
        start_deepseek=start_deepseek,
        stop_deepseek=stop_deepseek,
        start_validation_workers=start_validation_workers,
        stop_validation_workers=stop_validation_workers,
        stop_transient_workers=stop_transient_workers,
    )

    with (
        patch.object(config, "REALTIME_MARKET_SCHEDULER_ENABLED", True),
        patch.object(config, "DEEPSEEK_INTERNAL_SCHEDULER_ENABLED", True),
        patch.object(config, "WEB_BACKGROUND_WORKERS_ENABLED", True),
    ):
        assert supervisor.start() is True
        assert supervisor.start() is False
        assert supervisor.status()["owns_validation_workers"] is True
        supervisor.stop(timeout_seconds=0.25)

    assert realtime.start_calls == 1
    assert realtime.stop_calls == [0.25]
    start_deepseek.assert_called_once_with()
    stop_deepseek.assert_called_once_with(0.25)
    start_validation_workers.assert_called_once_with()
    stop_validation_workers.assert_called_once_with(0.25)
    stop_transient_workers.assert_called_once_with(0.25)
    assert supervisor.status()["started"] is False
    assert supervisor.status()["manages_transient_workers"] is True


def test_runtime_supervisor_continues_stopping_after_component_failure():
    realtime = _Scheduler()
    stop_transient_workers = Mock(side_effect=RuntimeError("stop failed"))
    stop_deepseek = Mock()
    supervisor = RuntimeSupervisor(
        realtime,
        start_deepseek=Mock(return_value=True),
        stop_deepseek=stop_deepseek,
        stop_transient_workers=stop_transient_workers,
    )

    with (
        patch.object(config, "REALTIME_MARKET_SCHEDULER_ENABLED", True),
        patch.object(config, "DEEPSEEK_INTERNAL_SCHEDULER_ENABLED", True),
        patch.object(config, "WEB_BACKGROUND_WORKERS_ENABLED", False),
        patch("stock_analyzer.runtime._LOGGER.exception") as log_exception,
    ):
        supervisor.start()
        supervisor.stop(timeout_seconds=0.25)

    stop_deepseek.assert_called_once_with(0.25)
    assert realtime.stop_calls == [0.25]
    log_exception.assert_called_once()


def test_runtime_supervisor_does_not_start_disabled_validation_workers():
    start_validation_workers = Mock(return_value=True)
    supervisor = RuntimeSupervisor(
        None,
        start_deepseek=Mock(return_value=False),
        start_validation_workers=start_validation_workers,
    )

    with (
        patch.object(config, "REALTIME_MARKET_SCHEDULER_ENABLED", False),
        patch.object(config, "DEEPSEEK_INTERNAL_SCHEDULER_ENABLED", False),
        patch.object(config, "WEB_BACKGROUND_WORKERS_ENABLED", False),
    ):
        supervisor.start()

    start_validation_workers.assert_not_called()
    assert supervisor.status()["owns_validation_workers"] is False


def test_realtime_scheduler_can_stop_without_waiting_for_poll_interval():
    scheduler = RealtimeMarketScheduler(
        refresh_quote_groups=Mock(),
        refresh_full_market=Mock(return_value=False),
        quote_refresh_status=Mock(return_value={}),
        clear_quotes_cache=Mock(),
        clear_recommendation_cache=Mock(),
        clear_horizon_cache=Mock(),
    )
    with patch("stock_analyzer.app_container.realtime_refresh_profile", return_value={"active": False}):
        assert scheduler.start() is True
        scheduler.stop(timeout_seconds=1.0)

    assert scheduler.status()["running"] is False
    assert not hasattr(scheduler, "container")


def test_realtime_scheduler_restores_state_when_thread_start_fails():
    class FailingThread:
        def start(self):
            raise RuntimeError("thread unavailable")

        def is_alive(self):
            return False

    scheduler = RealtimeMarketScheduler(
        refresh_quote_groups=Mock(),
        refresh_full_market=Mock(return_value=False),
        quote_refresh_status=Mock(return_value={}),
        clear_quotes_cache=Mock(),
        clear_recommendation_cache=Mock(),
        clear_horizon_cache=Mock(),
        thread_factory=lambda **_kwargs: FailingThread(),
    )

    with pytest.raises(RuntimeError, match="thread unavailable"):
        scheduler.start()

    assert scheduler.status()["started"] is False
    assert scheduler.status()["running"] is False
    scheduler.stop(0.0)
