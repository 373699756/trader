"""Explicit optional Tushare-client inputs for isolated market fixtures."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import TypedDict

from tests.helpers.shared.configuration import RuntimeSettingsFixture
from trader.download.infra.tushare import TushareClient
from trader.download.infra.tushare_records import TushareSdk
from trader.infra.settings import RuntimeSettings


class _ClockOptions(TypedDict, total=False):
    cancel_requested: Callable[[], bool]
    monotonic: Callable[[], float]
    wall_clock: Callable[[], datetime]


class TushareClientFixture(TushareClient):
    def __init__(
        self,
        *,
        token: str,
        timeout_seconds: float,
        sdk_factory: Callable[[str, float], TushareSdk],
        points: int = 2_000,
        settings: RuntimeSettings | None = None,
        circuit_breaker_failures: int | None = None,
        circuit_breaker_seconds: float | None = None,
        cancel_requested: Callable[[], bool] | None = None,
        monotonic: Callable[[], float] | None = None,
        wall_clock: Callable[[], datetime] | None = None,
    ) -> None:
        if circuit_breaker_failures is None or circuit_breaker_seconds is None:
            settings = settings if settings is not None else RuntimeSettingsFixture()
            if circuit_breaker_failures is None:
                circuit_breaker_failures = settings.market_data.circuit_breaker_failures
            if circuit_breaker_seconds is None:
                circuit_breaker_seconds = settings.market_data.circuit_breaker_seconds
        options: _ClockOptions = {}
        if cancel_requested is not None:
            options["cancel_requested"] = cancel_requested
        if monotonic is not None:
            options["monotonic"] = monotonic
        if wall_clock is not None:
            options["wall_clock"] = wall_clock
        super().__init__(
            token=token, points=points, timeout_seconds=timeout_seconds, sdk_factory=sdk_factory,
            circuit_breaker_failures=circuit_breaker_failures, circuit_breaker_seconds=circuit_breaker_seconds,
            **options,
        )
