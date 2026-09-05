from __future__ import annotations

from tests.component.market_data_test_support import (
    _SHANGHAI,
    AFTERNOON,
    LONG_POLICY,
    MARKET_REGIME_POLICY,
    NEWS_POLICY,
    NOW,
    TAIL_POLICY,
    Board,
    BoundedExecutor,
    BoundedLruCache,
    ChinaTradingCalendar,
    CountingHistoryClient,
    DataPlaneRecoverySummary,
    DataPlaneRepository,
    DataPlaneUnavailableError,
    FakeTushareFrame,
    FakeTusharePro,
    FeatureBuilder,
    Iterator,
    Mapping,
    MarketDataGateway,
    MutableMonotonic,
    Path,
    SecurityMasterRecord,
    SequenceMarketClient,
    SourceCursorRecord,
    SourceLaneRegistry,
    SourceObservation,
    StaticGateway,
    StaticHistoryClient,
    StaticMarketClient,
    StaticTencentClient,
    TradingCalendarUnavailableError,
    TushareClient,
    _history_bars,
    _quote,
    _service,
    _tushare_health,
    date,
    datetime,
    json,
    load_runtime_settings,
    pytest,
    replace,
    threading,
    time,
    timedelta,
)


def test_scheduled_reference_refresh_uses_bounded_history_warmup_instead_of_full_duplicate_batch() -> None:
    pool = BoundedExecutor(worker_count=5, queue_capacity=5, thread_name_prefix="source-data")
    lanes = SourceLaneRegistry(pool)

    class ReferenceGateway(StaticGateway):
        @staticmethod
        def update_reference_observations(_observations):
            return None

    class BlockingTushareClient:
        def __init__(self) -> None:
            self.started = threading.Event()
            self.release = threading.Event()

        def fetch_security_master(self, _observed_at):
            self.started.set()
            assert self.release.wait(1.0)
            return ()

        @staticmethod
        def supports(dataset):
            return dataset != "forward_adjusted_daily"

        @staticmethod
        def health():
            return _tushare_health(enabled=False, history_mode="raw")

        @staticmethod
        def fetch_forward_adjusted_daily(*_args):
            return ()

        @staticmethod
        def fetch_daily_valuations(*_args):
            return ()

        @staticmethod
        def fetch_financial_indicators(*_args):
            return ()

    class RecordingHistoryClient:
        def __init__(self) -> None:
            self.started = threading.Event()
            self.release = threading.Event()
            self.calls: list[str] = []

        def fetch_history(self, code, *, days):
            assert days == 61
            self.calls.append(code)
            self.started.set()
            assert self.release.wait(1.0)
            return ()

    tushare = BlockingTushareClient()
    history = RecordingHistoryClient()
    service = _service(
        ReferenceGateway((_quote(),)),
        history,
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY),
        tushare_client=tushare,
        worker_pool=pool,
        source_lanes=lanes,
        history_warmup_batch_size=1,
        wall_clock=lambda: NOW,
    )
    pool.start()

    try:
        service.schedule_reference_data(("600001", "600002"), NOW)
        assert tushare.started.wait(1.0)
        assert history.started.wait(0.2)
        assert history.calls == ["600001"]
    finally:
        history.release.set()
        tushare.release.set()
        lanes.stop(wait=True, timeout_seconds=1.0)
        pool.stop(wait=True, cancel_futures=True)


def test_reference_loader_recover_restores_security_master_and_calendar_cursor(tmp_path: Path) -> None:
    class CapturingGateway(StaticGateway):
        def __init__(self) -> None:
            super().__init__(())
            self.reference_observations: list[tuple[SourceObservation, ...]] = []

        def update_reference_observations(self, observations) -> None:
            self.reference_observations.append(tuple(observations))

    observed_at = datetime(2026, 7, 16, 9, 30, tzinfo=_SHANGHAI)
    source_time = observed_at.replace(minute=29)
    data_plane = DataPlaneRepository(tmp_path)
    data_plane.save_security_master_recent(
        SecurityMasterRecord(
            code="600001",
            observed_at=observed_at,
            source_time=source_time,
            source="tushare",
            data_version="tushare-calendar-current",
            payload={"board": "main", "listing_date": "2026-01-02"},
            payload_hash="",
            schema_version="data_plane",
        )
    )
    data_plane.save_source_cursor_recent(
        SourceCursorRecord(
            cursor_name="tushare.trading_calendar",
            cursor_value="2026-07-15",
            observed_at=observed_at,
            source_time=source_time,
            source="tushare",
            data_version="tushare-calendar-current",
            payload={
                "count": 2,
                "end_date": "2026-07-16",
                "sessions": [
                    {
                        "calendar_date": "2026-07-15",
                        "exchange": "SSE",
                        "is_open": True,
                        "pretrade_date": "2026-07-14",
                    },
                    {
                        "calendar_date": "2026-07-16",
                        "exchange": "SSE",
                        "is_open": True,
                        "pretrade_date": "2026-07-15",
                    },
                ],
            },
            payload_hash="",
            schema_version="data_plane",
        )
    )
    gateway = CapturingGateway()
    service = _service(
        gateway,
        StaticHistoryClient(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY),
        data_plane=data_plane,
        wall_clock=lambda: NOW,
    )

    assert service.references.recover() == DataPlaneRecoverySummary()
    assert gateway.reference_observations != []
    assert gateway.reference_observations[0][0].subject_key == "600001"
    assert tuple(item.subject_key for item in gateway.reference_observations[1]) == ("2026-07-15", "2026-07-16")
    assert all(item.fields["is_open"] is True for item in gateway.reference_observations[1])
    assert service.references._next_calendar_start(date(2026, 7, 10)) == date(2026, 7, 15)
    assert service.references._next_calendar_start(date(2026, 7, 20)) == date(2026, 7, 20)


def test_reference_loader_persists_full_market_free_security_master_once_per_payload(tmp_path: Path) -> None:
    observed_at = datetime(2026, 7, 16, 9, 30, tzinfo=_SHANGHAI)
    masters = tuple(
        SourceObservation(
            source="eastmoney_security_master",
            subject_key=code,
            observed_at=observed_at,
            source_time=observed_at,
            received_at=observed_at,
            effective_at=observed_at,
            data_version=f"eastmoney-master-{code}",
            fields={"board": "main", "exchange": "sse", "listing_date": "2020-01-02"},
            missing_reasons={},
            payload_hash=code * 10 + "0000",
            status="success",
            error_code=None,
        )
        for code in ("600001", "600002")
    )

    class ReferenceGateway(StaticGateway):
        @staticmethod
        def reference_observations(codes):
            selected = set(codes)
            return tuple(master for master in masters if master.subject_key in selected)

        @staticmethod
        def update_reference_observations(_observations):
            return None

    data_plane = DataPlaneRepository(tmp_path)
    service = _service(
        ReferenceGateway((_quote(),)),
        StaticHistoryClient(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY),
        data_plane=data_plane,
        wall_clock=lambda: observed_at,
    )

    service.references.schedule_reference_data(
        ("600001",),
        observed_at - timedelta(minutes=1),
        security_master_codes=("600001", "600002"),
    )
    service.references.schedule_reference_data(
        ("600001",),
        observed_at + timedelta(minutes=1),
        security_master_codes=("600001", "600002"),
    )

    persisted = data_plane.load_security_master_recent_records()
    assert tuple(record.code for record in persisted) == ("600001", "600002")
    assert all(record.source == "eastmoney_security_master" for record in persisted)
    assert all(record.observed_at == observed_at for record in persisted)
    assert all(dict(record.payload) == dict(masters[0].fields) for record in persisted)


def test_reference_loader_persists_cumulative_calendar_sessions(tmp_path: Path) -> None:
    observed_at = datetime(2026, 7, 16, 9, 30, tzinfo=_SHANGHAI)
    data_plane = DataPlaneRepository(tmp_path)
    service = _service(
        StaticGateway(()),
        StaticHistoryClient(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY),
        data_plane=data_plane,
        wall_clock=lambda: observed_at,
    )

    def calendar_observation(calendar_date: str, *, status: str = "success") -> SourceObservation:
        received_at = observed_at + timedelta(days=int(calendar_date[-2:]) - 15)
        return SourceObservation(
            source="tushare",
            subject_key=calendar_date,
            observed_at=received_at,
            source_time=received_at,
            received_at=received_at,
            effective_at=received_at,
            data_version=f"calendar-{calendar_date}",
            fields={"calendar_date": calendar_date, "exchange": "SSE", "is_open": True},
            missing_reasons={},
            payload_hash=calendar_date,
            status=status,
            error_code=None if status == "success" else "fixture_failure",
        )

    first = calendar_observation("2026-07-15")
    second = calendar_observation("2026-07-16")
    failed = calendar_observation("2026-07-17", status="failed")
    service.references._persist_trading_calendar(first.observed_at, (first,))
    service.references._persist_trading_calendar(second.observed_at, (second,))
    service.references._persist_trading_calendar(failed.observed_at, (failed,))

    record = data_plane.load_source_cursor_recent_records(cursor_names=("tushare.trading_calendar",))[0]
    assert record.cursor_value == "2026-07-16"
    assert record.payload["count"] == 2
    sessions = record.payload["sessions"]
    assert isinstance(sessions, tuple)
    assert tuple(item["calendar_date"] for item in sessions if isinstance(item, Mapping)) == (
        "2026-07-15",
        "2026-07-16",
    )


def test_reference_loader_recover_isolation_of_unavailable_data_plane() -> None:
    class UnavailableDataPlane:
        @staticmethod
        def recover() -> DataPlaneRecoverySummary:
            raise DataPlaneUnavailableError("unavailable")

        @staticmethod
        def load_security_master_recent_records(codes: list[str] | None = None) -> tuple[SecurityMasterRecord, ...]:
            raise AssertionError("must not read records when recovery fails")

        @staticmethod
        def load_source_cursor_recent_records(
            cursor_names: list[str] | None = None,
        ) -> tuple[SourceCursorRecord, ...]:
            raise AssertionError("must not read cursor rows when recovery fails")

    gateway = StaticGateway((_quote(),))
    service = _service(
        gateway,
        StaticHistoryClient(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY),
        data_plane=UnavailableDataPlane(),
        wall_clock=lambda: NOW,
    )

    summary = service.references.recover()
    assert summary == DataPlaneRecoverySummary()


def test_newer_reference_refresh_can_correct_an_older_effective_listing_date() -> None:
    gateway = MarketDataGateway(
        StaticMarketClient((replace(_quote(), source="eastmoney"),)),
        StaticMarketClient((replace(_quote(), source="sina"),)),
        StaticTencentClient(()),
        minimum_market_rows=1,
        circuit_breaker_failures=3,
        circuit_breaker_seconds=60,
        wall_clock=lambda: NOW,
    )

    def master(listing_date: str, source_time: datetime, version: str) -> SourceObservation:
        return SourceObservation(
            source="tushare",
            subject_key="600001",
            observed_at=source_time,
            source_time=source_time,
            received_at=source_time,
            effective_at=datetime.fromisoformat(f"{listing_date}T00:00:00+08:00"),
            data_version=version,
            fields={"board": "main", "listing_date": listing_date},
            missing_reasons={},
            payload_hash=version,
            status="success",
            error_code=None,
        )

    gateway.update_reference_observations((master("2020-01-02", NOW - timedelta(seconds=1), "master-initial"),))
    gateway.update_reference_observations((master("2019-01-02", NOW, "master-latest"),))
    gateway.fetch_market(observed_at=NOW)

    snapshot = gateway.canonical_snapshot()
    assert snapshot is not None
    assert snapshot.quotes[0].listing_date == date(2019, 1, 2)


def test_listing_session_projection_reuses_a_sorted_calendar_index() -> None:
    gateway = MarketDataGateway(
        StaticMarketClient((replace(_quote(), source="eastmoney"),)),
        StaticMarketClient((replace(_quote(), source="sina"),)),
        StaticTencentClient(()),
        minimum_market_rows=1,
        circuit_breaker_failures=3,
        circuit_breaker_seconds=60,
        wall_clock=lambda: NOW,
    )

    def calendar(day: date) -> SourceObservation:
        effective_at = datetime.combine(day, datetime.min.time(), tzinfo=NOW.tzinfo)
        return SourceObservation(
            source="tushare",
            subject_key=day.isoformat(),
            observed_at=NOW,
            source_time=NOW,
            received_at=NOW,
            effective_at=effective_at,
            data_version="calendar-current",
            fields={"calendar_date": day.isoformat(), "is_open": True},
            missing_reasons={},
            payload_hash=day.isoformat(),
            status="success",
            error_code=None,
        )

    gateway.update_reference_observations((calendar(date(2020, 1, 2)), calendar(date(2026, 7, 16))))

    class NoIterationOpenDates(set[date]):
        def __iter__(self):
            raise AssertionError("listing-age projection must not rescan every calendar date per security")

    gateway._calendar_open_dates = NoIterationOpenDates((date(2020, 1, 2), date(2026, 7, 16)))
    master = SourceObservation(
        source="tushare",
        subject_key="600001",
        observed_at=NOW,
        source_time=NOW,
        received_at=NOW,
        effective_at=datetime.fromisoformat("2020-01-02T00:00:00+08:00"),
        data_version="master-initial",
        fields={"board": "main", "listing_date": "2020-01-02"},
        missing_reasons={},
        payload_hash="master-initial",
        status="success",
        error_code=None,
    )

    gateway.update_reference_observations((master,))
    quote = gateway.fetch_market(observed_at=NOW)[0]

    assert quote.listing_age_sessions == 2


def test_listing_session_projection_uses_injected_production_calendar() -> None:
    calendar_requests: list[bool] = []

    def listing_open_dates() -> tuple[date, ...]:
        calendar_requests.append(True)
        return (date(2020, 1, 2), date(2020, 1, 3), date(2026, 7, 16))

    gateway = MarketDataGateway(
        StaticMarketClient((replace(_quote(), source="eastmoney"),)),
        StaticMarketClient((replace(_quote(), source="sina"),)),
        StaticTencentClient(()),
        minimum_market_rows=1,
        circuit_breaker_failures=3,
        circuit_breaker_seconds=60,
        listing_open_dates=listing_open_dates,
        wall_clock=lambda: NOW,
    )
    master = SourceObservation(
        source="eastmoney_security_master",
        subject_key="600001",
        observed_at=NOW,
        source_time=NOW,
        received_at=NOW,
        effective_at=datetime.fromisoformat("2020-01-02T00:00:00+08:00"),
        data_version="master-initial",
        fields={"board": "main", "listing_date": "2020-01-02"},
        missing_reasons={},
        payload_hash="master-initial",
        status="success",
        error_code=None,
    )

    gateway.update_reference_observations((master,))
    quote = gateway.fetch_market(observed_at=NOW)[0]

    assert quote.listing_age_sessions == 3
    assert calendar_requests == [True]


def test_snapshot_metadata_copies_tushare_versions_under_service_lock() -> None:
    quote = _quote()
    gateway = MarketDataGateway(
        StaticMarketClient((quote,)),
        StaticMarketClient((replace(quote, source="sina"),)),
        StaticTencentClient(()),
        minimum_market_rows=1,
        circuit_breaker_failures=3,
        circuit_breaker_seconds=60,
        wall_clock=lambda: NOW,
    )
    gateway.fetch_market(observed_at=NOW)
    service = _service(
        gateway,
        StaticHistoryClient(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY),
        wall_clock=lambda: NOW,
    )

    class TrackingLock:
        held = False

        def __enter__(self):
            self.held = True
            return self

        def __exit__(self, _exc_type, _exc, _traceback):
            self.held = False

    class LockCheckedVersions(Mapping[str, str]):
        def __init__(self, lock: TrackingLock) -> None:
            self._lock = lock
            self._values = {"valuation": "valuation-source"}

        def __getitem__(self, key: str) -> str:
            return self._values[key]

        def __iter__(self) -> Iterator[str]:
            assert self._lock.held
            return iter(self._values)

        def __len__(self) -> int:
            return len(self._values)

    tracking_lock = TrackingLock()
    service.references._lock = tracking_lock
    service.references._reference_versions = LockCheckedVersions(tracking_lock)

    metadata = service.snapshot_metadata()

    assert dict(metadata.reference_versions) == {"valuation": "valuation-source"}


def test_reference_refresh_reuses_cache_and_refreshes_due_entries_inside_tushare_lane() -> None:
    runtime = load_runtime_settings(Path(__file__).parents[2] / "config" / "runtime.json")
    monotonic = MutableMonotonic()
    cache: BoundedLruCache[object] = BoundedLruCache(
        runtime.market_data.cache_policy,
        cadence_seconds=runtime.pipeline.cadence_seconds,
        monotonic=monotonic,
        wall_clock=lambda: NOW,
    )
    pool = BoundedExecutor(worker_count=5, queue_capacity=5, thread_name_prefix="source-data")
    lanes = SourceLaneRegistry(pool)

    class ReferencePro(FakeTusharePro):
        def trade_cal(self, **kwargs):
            self.calls.append(("trade_cal", kwargs))
            return FakeTushareFrame(
                [{"exchange": "SSE", "cal_date": "20260716", "is_open": 1, "pretrade_date": "20260715"}]
            )

    pro = ReferencePro(
        [
            {
                "ts_code": "600001.SH",
                "symbol": "600001",
                "name": "测试股份",
                "industry": "工业",
                "market": "主板",
                "exchange": "SSE",
                "list_status": "L",
                "list_date": "20200102",
            }
        ]
    )
    quote = _quote()
    gateway = MarketDataGateway(
        StaticMarketClient((quote,)),
        StaticMarketClient((quote,)),
        StaticTencentClient((quote,)),
        minimum_market_rows=1,
        circuit_breaker_failures=3,
        circuit_breaker_seconds=60,
        worker_pool=pool,
        source_lanes=lanes,
        cache=cache,
        source_contract_versions=runtime.market_data.source_contract_versions,
        config_version=runtime.config_version,
        monotonic=monotonic,
        wall_clock=lambda: NOW,
    )
    service = _service(
        gateway,
        StaticHistoryClient(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY),
        tushare_client=TushareClient(
            token="secret-token",
            timeout_seconds=8,
            sdk_factory=lambda _token, _timeout: pro,
            wall_clock=lambda: NOW,
        ),
        worker_pool=pool,
        source_lanes=lanes,
        cache=cache,
        source_contract_versions=runtime.market_data.source_contract_versions,
        config_version=runtime.config_version,
        wall_clock=lambda: NOW,
    )
    pool.start()

    try:
        lanes.submit("tushare", "reference-cycle-1", NOW, service.refresh_reference_data, (), NOW).result()
        lanes.submit("tushare", "reference-cycle-2", NOW, service.refresh_reference_data, (), NOW).result()
        monotonic.value = 86400.001
        lanes.submit("tushare", "reference-cycle-3", NOW, service.refresh_reference_data, (), NOW).result()
    finally:
        lanes.stop(wait=True, timeout_seconds=1.0)
        pool.stop(wait=True, cancel_futures=True)
        cache.stop()

    assert [name for name, _arguments in pro.calls] == ["stock_basic", "trade_cal", "stock_basic", "trade_cal"]
    status = cache.status().datasets["security_master_calendar"]["tushare"]
    assert status.entries == 2
    assert status.hit == 4
    assert lanes.status().lanes["tushare"].superseded_count == 0


def test_reference_degradation_replaces_same_version_verified_identity_conservatively() -> None:
    quote = replace(_quote(), source="eastmoney")
    gateway = MarketDataGateway(
        StaticMarketClient((quote,)),
        StaticMarketClient((replace(quote, source="sina"),)),
        StaticTencentClient(()),
        minimum_market_rows=1,
        circuit_breaker_failures=3,
        circuit_breaker_seconds=60,
        wall_clock=lambda: NOW,
    )
    fields = {
        "board": "main",
        "exchange": "SSE",
        "listing_date": "2020-01-02",
        "listing_age_sessions": 1000.0,
        "has_price_limit": True,
        "exchange_limit_pct": 10.0,
    }
    verified = SourceObservation(
        source="tushare",
        subject_key="600001",
        observed_at=NOW,
        source_time=NOW,
        received_at=NOW,
        effective_at=NOW,
        data_version="master-initial",
        fields=fields,
        missing_reasons={},
        payload_hash="z-verified",
        status="success",
        error_code=None,
    )
    degraded = replace(
        verified,
        fields={**fields, "board_reliability": "degraded", "reference_data_degraded": True},
        missing_reasons={"cache_refresh": "timeout"},
        payload_hash="a-degraded",
    )

    gateway.update_reference_observations((verified,))
    gateway.fetch_market(observed_at=NOW)
    gateway.update_reference_observations((degraded,))
    refreshed = gateway.fetch_market(observed_at=NOW)

    assert refreshed[0].board_reliability == "degraded"
    assert "board_identity_degraded" in refreshed[0].execution_restrictions


def test_reference_refresh_structures_tushare_history_valuation_and_financial_data() -> None:
    runtime = load_runtime_settings(Path(__file__).parents[2] / "config" / "runtime.json")
    cache: BoundedLruCache[object] = BoundedLruCache(
        runtime.market_data.cache_policy,
        cadence_seconds=runtime.pipeline.cadence_seconds,
        wall_clock=lambda: AFTERNOON,
    )
    pool = BoundedExecutor(worker_count=5, queue_capacity=5, thread_name_prefix="source-data")
    lanes = SourceLaneRegistry(pool)

    class SlowDataPro(FakeTusharePro):
        def trade_cal(self, **kwargs):
            self.calls.append(("trade_cal", kwargs))
            return FakeTushareFrame(
                [{"exchange": "SSE", "cal_date": "20260715", "is_open": 1, "pretrade_date": "20260714"}]
            )

        def pro_bar(self, **kwargs):
            self.calls.append(("pro_bar", kwargs))
            return FakeTushareFrame(
                [
                    {
                        "ts_code": "600001.SH",
                        "trade_date": "20260715",
                        "open": 10.0,
                        "close": 10.2,
                        "high": 10.3,
                        "low": 9.9,
                        "vol": 1000.0,
                        "amount": 1020000.0,
                        "pct_chg": 2.0,
                    }
                ]
            )

        def daily_basic(self, **kwargs):
            self.calls.append(("daily_basic", kwargs))
            return FakeTushareFrame([{"ts_code": "600001.SH", "trade_date": "20260715", "pe": 12.0, "pb": 1.5}])

        def fina_indicator(self, **kwargs):
            self.calls.append(("fina_indicator", kwargs))
            return FakeTushareFrame(
                [{"ts_code": "600001.SH", "ann_date": "20260715", "end_date": "20260630", "eps": 1.0}]
            )

    pro = SlowDataPro(
        [
            {
                "ts_code": "600001.SH",
                "symbol": "600001",
                "name": "测试股份",
                "industry": "工业",
                "market": "主板",
                "exchange": "SSE",
                "list_status": "L",
                "list_date": "20200102",
            }
        ]
    )
    gateway = MarketDataGateway(
        StaticMarketClient((_quote(),)),
        StaticMarketClient((_quote(),)),
        StaticTencentClient((_quote(),)),
        minimum_market_rows=1,
        circuit_breaker_failures=3,
        circuit_breaker_seconds=60,
        worker_pool=pool,
        source_lanes=lanes,
        cache=cache,
        source_contract_versions=runtime.market_data.source_contract_versions,
        config_version=runtime.config_version,
        wall_clock=lambda: AFTERNOON,
    )
    service = _service(
        gateway,
        StaticHistoryClient(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY),
        tushare_client=TushareClient(
            token="secret-token",
            timeout_seconds=8,
            sdk_factory=lambda _token, _timeout: pro,
            wall_clock=lambda: AFTERNOON,
        ),
        worker_pool=pool,
        source_lanes=lanes,
        cache=cache,
        source_contract_versions=runtime.market_data.source_contract_versions,
        config_version=runtime.config_version,
        wall_clock=lambda: AFTERNOON,
    )
    pool.start()

    try:
        service.refresh_reference_data(("600001",), AFTERNOON)
        service.refresh_reference_data(("600001",), AFTERNOON)
    finally:
        lanes.stop(wait=True, timeout_seconds=1.0)
        pool.stop(wait=True, cancel_futures=True)
        cache.stop()

    assert [name for name, _arguments in pro.calls] == [
        "stock_basic",
        "trade_cal",
        "pro_bar",
        "daily_basic",
        "fina_indicator",
    ]
    daily_basic_arguments = next(arguments for name, arguments in pro.calls if name == "daily_basic")
    assert daily_basic_arguments["trade_date"] == "20260715"
    history_entries = service.history.entries()
    assert history_entries["600001"].bars[-1].trade_date == "2026-07-15"
    assert history_entries["600001"].bars[-1].volume == 100_000.0
    assert history_entries["600001"].bars[-1].amount == 1_020_000_000.0
    reference_fields = service.references.fields(("600001",))
    assert reference_fields["600001"]["tushare_valuation_pe"] == 12.0
    assert reference_fields["600001"]["tushare_financial_eps"] == 1.0


def test_unadjusted_tushare_history_is_not_consumed_and_warmup_uses_qfq_fallback() -> None:
    quotes = (_quote(), _quote(code="300001"), _quote(code="688001"))

    class HistoryPro:
        def __init__(self) -> None:
            self.calls = 0

        def daily(self, **kwargs):
            self.calls += 1
            assert kwargs["ts_code"] == "000001.SZ"
            rows = [
                {
                    "ts_code": code,
                    "trade_date": (date(2026, 7, 15) - timedelta(days=index)).strftime("%Y%m%d"),
                    "open": 10.0,
                    "close": 10.5,
                    "high": 10.8,
                    "low": 9.9,
                    "vol": 1000.0,
                    "amount": 10500.0,
                    "pct_chg": 1.0,
                }
                for code in str(kwargs["ts_code"]).split(",")
                for index in range(60)
            ]
            return FakeTushareFrame(rows)

    pro = HistoryPro()
    history = CountingHistoryClient(_history_bars())
    pool = BoundedExecutor(worker_count=5, queue_capacity=5, thread_name_prefix="source-data")
    lanes = SourceLaneRegistry(pool)
    service = _service(
        StaticGateway(quotes),
        history,
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY),
        tushare_client=TushareClient(
            token="secret-token",
            timeout_seconds=8,
            points=120,
            sdk_factory=lambda _token, _timeout: pro,
            wall_clock=lambda: NOW,
        ),
        worker_pool=pool,
        source_lanes=lanes,
        history_warmup_batch_size=3,
        market_ttl_seconds=1,
        wall_clock=lambda: NOW,
    )
    pool.start()

    try:
        first = service.fetch_market_features(NOW, deadline=NOW + timedelta(seconds=1))
        assert all(feature.history_days == 0 for feature in first)
        service.schedule_reference_data(tuple(quote.code for quote in quotes), NOW)
        timeout_at = time.monotonic() + 1.0
        while service.health()["history_warmup_completed_count"] < len(quotes) and time.monotonic() < timeout_at:
            time.sleep(0.01)
        while pro.calls == 0 and time.monotonic() < timeout_at:
            time.sleep(0.01)
        second = service.fetch_market_features(NOW + timedelta(seconds=2), force=True)
    finally:
        lanes.stop(wait=True, timeout_seconds=1.0)
        pool.stop(wait=True, cancel_futures=True)

    assert all(feature.history_days == 60 for feature in second)
    assert service.health()["history_coverage_ratio"] == 1.0
    assert service.health()["history_warmup_completed_count"] == 3
    assert service.health()["history_warmup_last_source"] == "tencent"
    assert service.health()["history_warmup_timeout_count"] == 0
    assert service.health()["history_warmup_inflight_age_seconds"] is None
    assert service.health()["history_warmup_batch_timeout_seconds"] == 20.0
    assert sorted(history.calls) == sorted(quote.code for quote in quotes)
    assert pro.calls == 1
    assert service.references.health().history_mode == "unadjusted_daily"
    assert service.references.health().process_api_attempts_today == 1


def test_permission_denied_tushare_falls_back_to_batched_history_lane() -> None:
    codes = ("600001", "600002", "300001", "300002", "688001", "688002")
    quotes = tuple(_quote(code=code) for code in codes)

    class PermissionDeniedTushare:
        @staticmethod
        def health():
            return _tushare_health(
                enabled=True,
                history_mode="unadjusted_daily",
                degraded_reason="permission_denied",
            )

    history = CountingHistoryClient(_history_bars())
    pool = BoundedExecutor(worker_count=5, queue_capacity=5, thread_name_prefix="source-data")
    lanes = SourceLaneRegistry(pool)
    service = _service(
        StaticGateway(quotes),
        history,
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY),
        tushare_client=PermissionDeniedTushare(),
        worker_pool=pool,
        source_lanes=lanes,
        history_warmup_batch_size=3,
        market_ttl_seconds=1,
        wall_clock=lambda: NOW,
    )
    pool.start()

    try:
        service.fetch_market_features(NOW, deadline=NOW + timedelta(seconds=1))
        timeout_at = time.monotonic() + 1.0
        while service.health()["history_warmup_completed_count"] < len(codes) and time.monotonic() < timeout_at:
            time.sleep(0.01)
    finally:
        lanes.stop(wait=True, timeout_seconds=1.0)
        pool.stop(wait=True, cancel_futures=True)

    assert sorted(history.calls) == sorted(codes)
    assert service.health()["history_warmup_completed_count"] == len(codes)
    assert service.health()["history_warmup_last_source"] == "tencent"


def test_calendar_uses_cache_and_fails_closed(tmp_path) -> None:
    cache = tmp_path / "calendar.json"
    cache.write_text(
        json.dumps({"fetched_at": NOW.isoformat(), "dates": ["2026-07-16"]}),
        encoding="utf-8",
    )
    calendar = ChinaTradingCalendar(cache, now=lambda: NOW)

    assert calendar.is_trading_day(date(2026, 7, 16)) is True
    assert calendar.is_trading_day(date(2026, 7, 18)) is False
    assert calendar.open_dates() == (date(2026, 7, 16),)

    unavailable = ChinaTradingCalendar(
        tmp_path / "missing.json",
        fetcher=lambda: (_ for _ in ()).throw(RuntimeError("offline")),
        now=lambda: NOW,
    )
    with pytest.raises(TradingCalendarUnavailableError):
        unavailable.is_trading_day(date(2026, 7, 16))

    stale_cache = tmp_path / "stale-calendar.json"
    stale_cache.write_text(
        json.dumps({"fetched_at": (NOW - timedelta(days=31)).isoformat(), "dates": ["2026-07-16"]}),
        encoding="utf-8",
    )
    stale = ChinaTradingCalendar(
        stale_cache,
        fetcher=lambda: (_ for _ in ()).throw(RuntimeError("offline")),
        now=lambda: NOW,
    )
    with pytest.raises(TradingCalendarUnavailableError, match="cannot refresh"):
        stale.is_trading_day(date(2026, 7, 16))


def test_calendar_fetch_timeout_is_bounded(tmp_path) -> None:
    release = threading.Event()
    started = threading.Event()

    def slow_fetcher():
        started.set()
        release.wait(1.0)
        return (date(2026, 7, 16),)

    calendar = ChinaTradingCalendar(
        tmp_path / "timeout-calendar.json",
        fetcher=slow_fetcher,
        fetch_timeout_seconds=0.02,
        now=lambda: NOW,
    )
    began = time.monotonic()
    try:
        with pytest.raises(TradingCalendarUnavailableError, match="timed out"):
            calendar.is_trading_day(date(2026, 7, 16))
    finally:
        release.set()

    assert started.is_set()
    assert time.monotonic() - began < 0.5


def test_equal_quote_version_can_gain_new_tushare_board_metadata_from_cache_hit() -> None:
    runtime = load_runtime_settings(Path(__file__).parents[2] / "config" / "runtime.json")
    cache: BoundedLruCache[object] = BoundedLruCache(
        runtime.market_data.cache_policy,
        cadence_seconds=runtime.pipeline.cadence_seconds,
        wall_clock=lambda: NOW,
    )
    quote = replace(_quote(), source="eastmoney")
    gateway = MarketDataGateway(
        StaticMarketClient((quote,)),
        StaticMarketClient((replace(quote, source="sina"),)),
        StaticTencentClient(()),
        minimum_market_rows=1,
        circuit_breaker_failures=3,
        circuit_breaker_seconds=60,
        cache=cache,
        source_contract_versions=runtime.market_data.source_contract_versions,
        config_version=runtime.config_version,
        wall_clock=lambda: NOW,
    )
    first = gateway.fetch_market(observed_at=NOW)
    master = SourceObservation(
        source="tushare",
        subject_key="600001",
        observed_at=NOW,
        source_time=NOW,
        received_at=NOW,
        effective_at=NOW - timedelta(days=1),
        data_version="master-initial",
        fields={
            "board": "main",
            "exchange": "SSE",
            "listing_date": "2020-01-02",
            "listing_age_sessions": 1000.0,
            "has_price_limit": True,
            "exchange_limit_pct": 10.0,
        },
        missing_reasons={},
        payload_hash="master-initial",
        status="success",
        error_code=None,
    )
    gateway.update_reference_observations((master,))

    second = gateway.fetch_market(observed_at=NOW)

    assert first[0].board_source == "code_prefix_fallback"
    assert second[0].board_source == "tushare"
    assert second[0].board_reliability == "verified"
    assert second[0].execution_restrictions == ()


def test_gateway_retains_free_security_master_when_realtime_source_falls_back() -> None:
    eastmoney_quote = replace(
        _quote(),
        source="eastmoney",
        board=Board.MAIN,
        board_source="eastmoney",
        board_reliability="reported",
        exchange="SSE",
        listing_date=date(1999, 11, 10),
    )
    second_observed_at = NOW + timedelta(seconds=1)
    sina_quote = replace(
        _quote(),
        source="sina",
        source_time=second_observed_at,
        received_time=second_observed_at,
        data_version="sina-history",
    )
    gateway = MarketDataGateway(
        SequenceMarketClient(((eastmoney_quote,), RuntimeError("eastmoney offline"))),
        SequenceMarketClient(((sina_quote,),)),
        StaticTencentClient(()),
        minimum_market_rows=1,
        circuit_breaker_failures=3,
        circuit_breaker_seconds=30,
        wall_clock=lambda: second_observed_at,
    )

    first = tuple(gateway.fetch_market(observed_at=NOW))
    second = tuple(gateway.fetch_market(observed_at=second_observed_at, force=True))

    assert first[0].listing_date == date(1999, 11, 10)
    assert second[0].source == "sina"
    assert second[0].board is Board.MAIN
    assert second[0].board_source == "eastmoney"
    assert second[0].board_reliability == "reported"
    assert second[0].exchange == "SSE"
    assert second[0].listing_date == date(1999, 11, 10)


def test_newer_sparse_free_identity_cannot_delete_existing_listing_fields() -> None:
    gateway = MarketDataGateway(
        StaticMarketClient((replace(_quote(), source="eastmoney"),)),
        StaticMarketClient((replace(_quote(), source="sina"),)),
        StaticTencentClient(()),
        minimum_market_rows=1,
        circuit_breaker_failures=3,
        circuit_breaker_seconds=30,
        listing_open_dates=lambda: (date(2020, 1, 2), date(2020, 1, 3), date(2026, 7, 16)),
        wall_clock=lambda: NOW,
    )
    rich = SourceObservation(
        source="eastmoney_security_master",
        subject_key="600001",
        observed_at=NOW - timedelta(seconds=1),
        source_time=NOW - timedelta(seconds=1),
        received_at=NOW - timedelta(seconds=1),
        effective_at=NOW - timedelta(seconds=1),
        data_version="eastmoney-master-rich",
        fields={"board": "main", "exchange": "SSE", "listing_date": "2020-01-02"},
        missing_reasons={},
        payload_hash="rich-master",
        status="success",
        error_code=None,
    )
    sparse = replace(
        rich,
        observed_at=NOW,
        source_time=NOW,
        received_at=NOW,
        effective_at=NOW,
        data_version="eastmoney-master-sparse",
        fields={"board": "main", "exchange": "SSE"},
        payload_hash="sparse-master",
    )

    gateway.update_reference_observations((rich,))
    gateway.update_reference_observations((sparse,))
    reference = gateway.reference_observations(("600001",))[0]
    quote = gateway.fetch_market(observed_at=NOW)[0]
    security_master = gateway.health().security_master

    assert reference.data_version == "eastmoney-master-sparse"
    assert reference.fields["listing_date"] == "2020-01-02"
    assert reference.fields["listing_age_sessions"] == 3.0
    assert quote.listing_date == date(2020, 1, 2)
    assert quote.listing_age_sessions == 3
    assert security_master.total_rows == 1
    assert security_master.listing_date_rows == 1
    assert security_master.listing_age_rows == 1
    assert security_master.complete_rows == 1
    assert security_master.provider == "free_market+production_calendar"
    assert security_master.tushare_required is False
    assert security_master.persistence_schedule_error_count == 0
