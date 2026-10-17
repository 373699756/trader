from __future__ import annotations

from tests.component.market_data_test_support import (
    _SHANGHAI,
    AFTERNOON,
    FEATURE_WEIGHT_POLICY,
    LONG_POLICY,
    MARKET_REGIME_POLICY,
    NEWS_POLICY,
    NOW,
    RESEARCH_COMPONENT_IDS,
    TAIL_POLICY,
    AkshareResearchClient,
    BlockingStructuredResearchClient,
    BoundedExecutor,
    BoundedLruCache,
    CountingHistoryClient,
    DataPlaneUnavailableError,
    Evidence,
    FailingResearchClient,
    FakeResponse,
    FeatureBuilder,
    FinancialReport,
    LoadRuntimeSettings,
    PartiallyBlockingStructuredResearchClient,
    Path,
    PersistResearchComponentStatuses,
    ResearchObservation,
    RiskEvidenceRecord,
    SourceLaneScheduler,
    SQLiteDataPlane,
    StaticGateway,
    StaticHistoryClient,
    StaticIntradayClient,
    StaticResearchClient,
    StaticStructuredResearchClient,
    _history_bars,
    _quote,
    _service,
    _tail_minute_bars,
    date,
    datetime,
    json,
    pytest,
    replace,
    requests,
    threading,
    time,
    timedelta,
    timezone,
)
from trader.recommendation.application.ports.runtime import RefreshOutcome
from trader.recommendation.application.runtime.cadence import PipelineTask
from trader.recommendation.domain.market.eligibility import IssuerEligibilityFact, IssuerEligibilityReason
from trader.recommendation.infra.market_data.market_cache_identity import (
    _deserialize_research_observation,
    _merge_research_observation,
    _serialize_research_observation,
)
from trader.recommendation.infra.market_data.market_feature_cache_entries import _ResearchEntry
from trader.recommendation.infra.persistence.issuer_eligibility import SQLiteIssuerEligibilityIndex


def test_history_intraday_and_research_share_the_bounded_market_cache() -> None:
    runtime = LoadRuntimeSettings(Path(__file__).parents[2] / "config" / "runtime.json")
    cache: BoundedLruCache[object] = BoundedLruCache(
        runtime.market_data.cache_policy,
        cadence_seconds=runtime.pipeline.cadence_seconds,
        wall_clock=lambda: NOW,
    )
    history = CountingHistoryClient(_history_bars())
    intraday = StaticIntradayClient(_tail_minute_bars())
    evidence = Evidence(
        evidence_id="news-1",
        evidence_type="news",
        title="中标",
        source="fixture",
        published_at=NOW - timedelta(hours=1),
        received_at=NOW - timedelta(minutes=59),
        data_version="news-source",
    )
    research = StaticResearchClient((evidence,))
    service = _service(
        StaticGateway((_quote(),)),
        history,
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY, FEATURE_WEIGHT_POLICY),
        intraday_client=intraday,
        research_client=research,
        cache=cache,
        source_contracts=runtime.market_data.source_contracts,
        config_version=runtime.config_version,
        wall_clock=lambda: NOW,
    )

    service.history.load(("600001",))
    service.intraday.load(("600001",), NOW)
    service.research.load(("600001",), NOW, include_structured=False)
    service.history.load(("600001",))
    service.intraday.load(("600001",), NOW)
    service.research.load(("600001",), NOW, include_structured=False)

    status = cache.status().datasets
    assert "daily_history" not in status
    assert status["intraday_minutes"]["eastmoney"].entries == 1
    assert status["intraday_minutes"]["eastmoney"].hit == 1
    assert status["research_success"]["akshare"].entries == 1
    assert status["research_success"]["akshare"].hit == 1
    assert history.calls == ["600001", "600001"]
    assert intraday.calls == ["600001"]
    assert research.calls == 1


def test_level_one_exclusion_prunes_every_non_frozen_per_stock_data_request(tmp_path: Path, monkeypatch) -> None:
    class RecordingGateway(StaticGateway):
        def __init__(self, quotes) -> None:
            super().__init__(quotes)
            self.candidate_requests = []
            self.long_requests = []

        def fetch_candidates(self, codes, **kwargs):
            self.candidate_requests.append(tuple(codes))
            return super().fetch_candidates(codes, **kwargs)

        def fetch_long_quotes(self, codes, **_kwargs):
            self.long_requests.append(tuple(codes))
            requested = set(codes)
            return tuple(quote for quote in self._quotes if quote.code in requested)

    registry = SQLiteIssuerEligibilityIndex(tmp_path / "filter_config")
    registry.record(
        (
            IssuerEligibilityFact(
                "600001",
                IssuerEligibilityReason.HISTORICAL_ST,
                NOW,
                "quote:600001:st-source",
                "eastmoney_market",
                "a" * 64,
            ),
        )
    )
    gateway = RecordingGateway((_quote("600001"), _quote("600002")))
    history = CountingHistoryClient(_history_bars())
    research = StaticStructuredResearchClient(
        Evidence("news-1", "news", "普通研究", "fixture", NOW),
        ResearchObservation(announcements_available=True, pledge_ratio_pct=0.0, unlock_ratio_pct=0.0),
    )
    intraday = StaticIntradayClient(_tail_minute_bars())
    service = _service(
        gateway,
        history,
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY, FEATURE_WEIGHT_POLICY),
        eligibility=registry,
        research_client=research,
        intraday_client=intraday,
    )
    reference_requests = []

    def record_reference(codes, _observed_at, *, force, security_master_codes):
        reference_requests.append((tuple(codes), force, tuple(security_master_codes)))

    monkeypatch.setattr(service.references, "schedule_reference_data", record_reference)

    batch = service.fetch_market_feature_batch(NOW)
    market = batch.features
    eligibility_batch = batch.issuer_eligibility

    assert tuple(item.quote.code for item in market) == ("600002",)
    assert eligibility_batch.input_count == 2
    assert eligibility_batch.eligible_count == 1
    assert [(item.reason, item.count) for item in eligibility_batch.reason_counts] == [
        (IssuerEligibilityReason.HISTORICAL_ST, 1)
    ]
    assert tuple((item.input_count, item.output_count) for item in batch.static_stages) == (
        (2, 2),
        (2, 2),
        (2, 2),
        (2, 1),
    )
    assert batch.static_stages[3].rejected_count == 1
    assert batch.static_stages[3].reasons[0].code == "historical_st"
    cached_batch = service.fetch_market_feature_batch(NOW)
    assert cached_batch.static_stages[3].rejected_count == 1
    assert cached_batch.static_stages[3].input_count == 2
    assert cached_batch.static_stages[1].reasons[0].code == "static_baseline_reused"

    service.refresh_candidate_quotes(("600001", "600002"), NOW)
    research_result = service.refresh_stock_risk(("600001", "600002"), NOW)
    assert len(research_result.data_version) == len("research-batch:") + 64
    assert research_result.completed_at is not None and research_result.completed_at.tzinfo == _SHANGHAI
    RefreshOutcome(
        PipelineTask.RESEARCH_SNAPSHOT,
        bool(research_result.changed_codes),
        research_result.data_version,
        research_result.changed_codes,
        research_result.completed_at or NOW,
        False,
    )
    service.refresh_intraday_tail(("600001", "600002"), NOW)
    service.refresh_long_quotes(("600001", "600002"), NOW)
    service.schedule_reference_data(
        ("600001", "600002"),
        NOW,
        security_master_codes=("600001", "600002"),
    )

    assert gateway.candidate_requests == [("600002",)]
    assert gateway.long_requests == [("600002",)]
    assert research.news_calls == 0
    assert research.snapshot_calls == 1
    assert intraday.calls == ["600002"]
    assert reference_requests == [(("600002",), False, ("600002",))]
    assert "600001" not in history.calls


def test_latest_disclosed_report_loss_is_derived_without_permanent_fact(tmp_path: Path) -> None:
    class RecordingGateway(StaticGateway):
        def __init__(self, quotes) -> None:
            super().__init__(quotes)
            self.candidate_requests = []

        def fetch_candidates(self, codes, **kwargs):
            self.candidate_requests.append(tuple(codes))
            return super().fetch_candidates(codes, **kwargs)

    latest_loss = FinancialReport(
        report_date=date(2026, 6, 30),
        published_at=NOW - timedelta(days=1),
        parent_net_profit=-1.0,
        core_net_profit=2.0,
    )
    research = StaticStructuredResearchClient(
        Evidence("news-1", "news", "普通研究", "fixture", NOW),
        ResearchObservation(
            financial_history=(latest_loss,),
            financial_history_complete=True,
            announcements_available=True,
        ),
    )
    registry = SQLiteIssuerEligibilityIndex(tmp_path / "filter_config")
    gateway = RecordingGateway((_quote("600001"),))
    history = CountingHistoryClient(_history_bars())
    service = _service(
        gateway,
        history,
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY, FEATURE_WEIGHT_POLICY),
        eligibility=registry,
        research_client=research,
    )

    result = service.fetch_candidate_features(("600001",), NOW, include_structured_research=True)

    assert tuple(feature.quote.code for feature in result) == ("600001",)
    assert result[0].values["latest_disclosed_report_loss"] == 1.0
    assert "issuer_risk:historical_audited_loss" not in result[0].quote.execution_restrictions
    assert gateway.candidate_requests == [("600001",)]
    assert history.calls == ["600001", "600001"]
    assert registry.exclusions(NOW) == ()


def test_source_lane_research_deadline_discards_late_memory_and_disk_cache(tmp_path) -> None:
    runtime = LoadRuntimeSettings(Path(__file__).parents[2] / "config" / "runtime.json")
    cache: BoundedLruCache[object] = BoundedLruCache(
        runtime.market_data.cache_policy,
        cadence_seconds=runtime.pipeline.cadence_seconds,
    )
    pool = BoundedExecutor(worker_count=5, queue_capacity=5, thread_name_prefix="source-data")
    lanes = SourceLaneScheduler(pool)
    research = BlockingStructuredResearchClient()
    service = _service(
        StaticGateway((_quote(),)),
        StaticHistoryClient(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY, FEATURE_WEIGHT_POLICY),
        research_client=research,
        research_cache_dir=tmp_path,
        worker_pool=pool,
        source_lanes=lanes,
        cache=cache,
        source_contracts=runtime.market_data.source_contracts,
        config_version=runtime.config_version,
    )
    pool.start()
    deadline = datetime.now(timezone.utc) + timedelta(seconds=0.01)
    release_timer = threading.Timer(0.6, research.release.set)
    release_timer.start()

    started = time.monotonic()
    try:
        result = service.refresh_stock_risk(("600001",), NOW, deadline=deadline)
        assert result.deadline_reached
        assert result.deferred_codes == ("600001",)
        elapsed = time.monotonic() - started
    finally:
        research.release.set()
        release_timer.cancel()
        lanes.stop(wait=True, timeout_seconds=1.0)
        pool.stop(wait=True, cancel_futures=True)
        cache.stop()

    assert elapsed < 0.5
    assert service.research.entries() == {}
    research_cache_status = cache.status().datasets.get("research_success", {}).get("akshare")
    assert research_cache_status is None or research_cache_status.entries == 0
    assert not (tmp_path / "observations").exists()


def test_akshare_news_is_bounded_and_normalized() -> None:
    callback = "diag"
    payload = {
        "result": {
            "cmsArticleWebOld": [
                {"title": "时间未知", "date": "invalid", "mediaName": "交易所"},
                {"title": "未来新闻", "date": "2026-07-16 11:00:00", "mediaName": "交易所"},
                {"title": "过期新闻", "date": "2026-07-08 09:00:00", "mediaName": "交易所"},
                {
                    "title": "<em>测试股份</em>发布公告",
                    "date": "2026-07-16 09:00:00",
                    "mediaName": "交易所",
                },
            ]
        }
    }
    calls = []

    def get(*args, **kwargs):
        calls.append((args, kwargs))
        return FakeResponse(f"{callback}({json.dumps(payload, ensure_ascii=False)});")

    evidence = AkshareResearchClient(timeout_seconds=8, get=get).fetch_news("600001", observed_at=NOW, limit=5)

    assert len(evidence) == 1
    assert evidence[0].evidence_type == "news"
    assert evidence[0].title == "测试股份发布公告"
    assert evidence[0].published_at.isoformat() == "2026-07-16T09:00:00+08:00"
    assert calls[0][1]["timeout"] == 8
    assert calls[0][1]["proxies"] == {"http": "", "https": "", "all": ""}
    assert calls[0][1]["headers"]["User-Agent"] == "Mozilla/5.0"
    assert "_" not in calls[0][1]["params"]


def test_refreshed_window_replaces_old_news_and_ordinary_announcement_evidence() -> None:
    old_evidence = (
        Evidence("old-news", "news", "旧新闻", "eastmoney_news", NOW - timedelta(days=10)),
        Evidence("old-ann", "announcement", "旧公告", "eastmoney_announcement", NOW - timedelta(days=200)),
        Evidence("official-risk", "regulatory_filing", "已确认风险", "cninfo", NOW - timedelta(days=200)),
    )
    current_evidence = (Evidence("new-news", "news", "新新闻", "eastmoney_news", NOW),)
    old = ResearchObservation(announcements_available=True, news_queried=True, evidence=old_evidence)
    current = ResearchObservation(
        queried_on=NOW.date(),
        announcements_available=True,
        news_queried=True,
        evidence=current_evidence,
    )

    merged = _merge_research_observation(_ResearchEntry(old, 0.0), current)

    assert {item.evidence_id for item in merged.evidence} == {"new-news", "official-risk"}


def test_research_window_dates_survive_daily_checkpoint_and_legacy_rows_refresh() -> None:
    current = ResearchObservation(queried_on=NOW.date(), pledge_checked_on=NOW.date(), news_queried=True)
    payload = _serialize_research_observation(current)

    assert _deserialize_research_observation(payload) == current
    del payload["queried_on"]
    del payload["pledge_checked_on"]
    legacy = _deserialize_research_observation(payload)
    assert legacy.queried_on is None
    assert legacy.pledge_checked_on is None


def test_akshare_news_response_is_cached_with_atomic_writer(tmp_path) -> None:
    callback = "diag"
    payload = {
        "result": {
            "cmsArticleWebOld": [
                {
                    "title": "<em>测试股份</em>发布公告",
                    "date": "2026-07-16 09:00:00",
                    "mediaName": "交易所",
                },
            ]
        }
    }

    def get(*args, **kwargs):
        return FakeResponse(f"{callback}({json.dumps(payload, ensure_ascii=False)});")

    AkshareResearchClient(
        timeout_seconds=8,
        get=get,
        evidence_cache_dir=tmp_path,
    ).fetch_news("600001", observed_at=NOW)

    cached = json.loads((tmp_path / "raw" / "news" / "600001.json").read_text(encoding="utf-8"))

    assert cached["source"] == "news"
    assert cached["code"] == "600001"
    assert "payload" in cached


def test_akshare_risk_batch_fetches_structured_fields_without_claiming_regulatory_history() -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    def get(url, **kwargs):
        params = kwargs["params"]
        calls.append((url, params))
        if "securities/api/data/get" in url:
            assert '"600001.SH"' in params["filter"]
            assert '"830001.BJ"' in params["filter"]
            return FakeResponse(
                {
                    "success": True,
                    "result": {
                        "pages": 1,
                        "count": 2,
                        "data": [
                            {
                                "SECURITY_CODE": code,
                                "REPORT_DATE": "2026-03-31",
                                "NOTICE_DATE": "2026-04-30",
                                "EPSJB": "1",
                                "PARENTNETPROFIT": "10",
                                "KCFJCXSYJLR": "8",
                            }
                            for code in ("600001", "830001")
                        ],
                    },
                }
            )
        if params.get("reportName") == "RPT_CSDC_LIST":
            return FakeResponse(
                {
                    "success": True,
                    "result": {
                        "pages": 1,
                        "count": 1,
                        "data": [
                            {
                                "SECURITY_CODE": "600001",
                                "TRADE_DATE": "2026-07-16",
                                "PLEDGE_RATIO": "5",
                            }
                        ],
                    },
                }
            )
        if params.get("reportName") == "RPT_LIFT_STAGE":
            return FakeResponse(
                {
                    "success": True,
                    "result": {
                        "pages": 1,
                        "count": 1,
                        "data": [
                            {
                                "SECURITY_CODE": "830001",
                                "FREE_DATE": "2026-08-01",
                                "TOTAL_RATIO": "0.02",
                            }
                        ],
                    },
                }
            )
        raise AssertionError(f"unexpected research URL: {url}")

    observations = AkshareResearchClient(
        timeout_seconds=8,
        get=get,
        long_research_policy=LONG_POLICY,
    ).fetch_risk_snapshots(("600001", "830001"), observed_at=AFTERNOON)

    assert set(observations) == {"600001", "830001"}
    assert observations["600001"].financial is not None
    assert observations["830001"].financial is not None
    assert all(not item.announcements_available for item in observations.values())
    assert all(item.announcements == () for item in observations.values())
    assert all(not item.corporate_risk_history_complete for item in observations.values())
    assert all("announcements:deferred" in item.source_errors for item in observations.values())
    assert observations["600001"].pledge_ratio_pct == pytest.approx(5.0)
    assert observations["830001"].pledge_ratio_pct == pytest.approx(0.0)
    assert observations["600001"].unlock_ratio_pct == pytest.approx(0.0)
    assert observations["830001"].unlock_ratio_pct == pytest.approx(2.0)
    assert not any("search-api-web" in url for url, _params in calls)
    pledge_filters = [str(params["filter"]) for _url, params in calls if params.get("reportName") == "RPT_CSDC_LIST"]
    assert pledge_filters == [
        "(TRADE_DATE<='2026-07-16')",
        '(TRADE_DATE=\'2026-07-16\')(SECURITY_CODE in ("600001","830001"))',
    ]
    unlock_filter = next(
        str(params["filter"]) for _url, params in calls if params.get("reportName") == "RPT_LIFT_STAGE"
    )
    assert "(FREE_DATE>='2026-07-16')" in unlock_filter
    assert "(FREE_DATE<='2026-10-14')" in unlock_filter


def test_research_cache_is_used_after_restart_before_source_request(tmp_path) -> None:
    cache_dir = tmp_path / "evidence_cache"
    cache_file = cache_dir / "observations" / "news" / "600001.json"
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(
        json.dumps(
            {
                "include_structured": False,
                "expires_at": (NOW + timedelta(minutes=10)).isoformat(),
                "observation": {
                    "financial": None,
                    "announcements": (),
                    "announcements_available": False,
                    "pledge_ratio_pct": None,
                    "unlock_ratio_pct": None,
                    "evidence": [
                        {
                            "evidence_id": "cached-news:1",
                            "evidence_type": "news",
                            "title": "缓存新闻",
                            "source": "eastmoney_news",
                            "published_at": "2026-07-16T09:00:00+08:00",
                            "received_at": "2026-07-16T09:05:00+08:00",
                            "data_version": "cached-initial",
                        }
                    ],
                    "source_errors": [],
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    service = _service(
        StaticGateway((_quote(),)),
        StaticHistoryClient(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY, FEATURE_WEIGHT_POLICY),
        research_client=FailingResearchClient(),
        research_cache_dir=cache_dir,
        research_workers=1,
        wall_clock=lambda: NOW,
    )
    result = service.fetch_candidate_features(("600001",), NOW)

    assert any(item.evidence_id == "cached-news:1" for item in result[0].evidence)


def test_research_cache_expired_calls_research_client(tmp_path) -> None:
    cache_dir = tmp_path / "evidence_cache"
    cache_file = cache_dir / "observations" / "news" / "600001.json"
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(
        json.dumps(
            {
                "include_structured": False,
                "expires_at": (NOW - timedelta(minutes=1)).isoformat(),
                "observation": {
                    "financial": None,
                    "announcements": (),
                    "announcements_available": False,
                    "pledge_ratio_pct": None,
                    "unlock_ratio_pct": None,
                    "evidence": [
                        {
                            "evidence_id": "stale-news:1",
                            "evidence_type": "news",
                            "title": "过期缓存",
                            "source": "eastmoney_news",
                            "published_at": "2026-07-16T09:00:00+08:00",
                            "received_at": "2026-07-16T09:05:00+08:00",
                            "data_version": "cached-initial",
                        }
                    ],
                    "source_errors": [],
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    research = StaticResearchClient(
        (Evidence("fresh-news", "news", "实时抓取", "eastmoney_news", NOW - timedelta(hours=1)),)
    )
    service = _service(
        StaticGateway((_quote(),)),
        StaticHistoryClient(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY, FEATURE_WEIGHT_POLICY),
        research_client=research,
        research_cache_dir=cache_dir,
        research_workers=1,
        wall_clock=lambda: NOW,
    )
    result = service.fetch_candidate_features(("600001",), NOW)

    assert research.calls == 1
    assert any(item.evidence_id == "fresh-news" for item in result[0].evidence)


def test_akshare_structured_research_is_point_in_time_and_builds_real_long_inputs(tmp_path) -> None:
    financial_payload = {
        "version": "financial-source",
        "result": {
            "count": 3,
            "data": [
                {
                    "REPORT_DATE": "2026-06-30 00:00:00",
                    "NOTICE_DATE": "2026-07-17 00:00:00",
                    "EPSJB": 9.0,
                    "BPS": 9.0,
                },
                {
                    "REPORT_DATE": "2026-03-31 00:00:00",
                    "NOTICE_DATE": "2026-04-30 00:00:00",
                    "EPSJB": 1.0,
                    "BPS": 10.0,
                    "TOTALOPERATEREVETZ": 20.0,
                    "PARENTNETPROFITTZ": 10.0,
                    "KCFJCXSYJLRTZ": 0.0,
                    "ROEJQ": 3.0,
                    "PARENTNETPROFIT": 100.0,
                    "KCFJCXSYJLR": 80.0,
                },
                {
                    "REPORT_DATE": "2022-12-31 00:00:00",
                    "NOTICE_DATE": "2023-04-20 00:00:00",
                    "PARENTNETPROFIT": -10.0,
                    "KCFJCXSYJLR": -12.0,
                },
            ],
        },
        "success": True,
    }
    announcement_payload = {
        "data": {
            "list": [
                {
                    "art_code": "future",
                    "display_time": "2026-07-17 09:00:00:000",
                    "notice_date": "2026-07-17 00:00:00",
                    "title": "未来公告",
                    "columns": [{"column_name": "重大事项"}],
                },
                {
                    "art_code": "a-1",
                    "display_time": "2026-07-15 10:00:00:000",
                    "notice_date": "2026-07-15 00:00:00",
                    "title": "控股股东减持并收到监管函",
                    "columns": [{"column_name": "持股变动"}],
                },
                {
                    "art_code": "a-2",
                    "display_time": "2026-07-14 10:00:00:000",
                    "notice_date": "2026-07-14 00:00:00",
                    "title": "公司获得政策支持并获批新项目",
                    "columns": [{"column_name": "重大事项"}],
                },
                *(
                    {
                        "art_code": f"normal-{index}",
                        "display_time": "2026-07-13 10:00:00:000",
                        "notice_date": "2026-07-13 00:00:00",
                        "title": f"公司日常经营公告{index}",
                        "columns": [{"column_name": "其他"}],
                    }
                    for index in range(20)
                ),
            ],
            "total_hits": 23,
        },
        "success": 1,
    }
    pledge_payload = {
        "version": "pledge-source",
        "result": {"data": [{"NOTICE_DATE": "2026-07-01", "TRADE_DATE": "2026-07-16", "PLEDGE_RATIO": 15.0}]},
        "success": True,
    }
    unlock_payload = {
        "version": "unlock-source",
        "result": {
            "data": [
                {"FREE_DATE": "2026-08-01", "TOTAL_RATIO": 0.06},
                {"FREE_DATE": "2027-01-01", "TOTAL_RATIO": 0.50},
            ]
        },
        "success": True,
    }
    calls = []

    def get(url, **kwargs):
        calls.append((url, kwargs))
        if "search-api-web" in url:
            callback = "diag"
            return FakeResponse(f'{callback}({{"result": {{"cmsArticleWebOld": []}}}});')
        if "securities/api/data/get" in url:
            return FakeResponse(financial_payload)
        if "api/security/ann" in url:
            return FakeResponse(announcement_payload)
        report = kwargs["params"].get("reportName")
        if report == "RPT_CSDC_LIST" and kwargs["params"]["filter"].startswith("(TRADE_DATE<="):
            return FakeResponse({"success": True, "result": {"data": [{"TRADE_DATE": "2026-07-16"}]}})
        if report == "RPT_CSDC_LIST":
            return FakeResponse(pledge_payload)
        if report == "RPT_LIFT_STAGE":
            return FakeResponse(unlock_payload)
        raise AssertionError(f"unexpected research URL: {url}")

    client = AkshareResearchClient(
        timeout_seconds=8,
        get=get,
        long_research_policy=LONG_POLICY,
        evidence_cache_dir=tmp_path,
    )
    observation = client.fetch_snapshot("600001", observed_at=AFTERNOON)
    repeated = client.fetch_snapshot("600001", observed_at=AFTERNOON + timedelta(minutes=1))
    client.fetch_snapshot("600001", observed_at=AFTERNOON + timedelta(minutes=11))
    feature = FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY, FEATURE_WEIGHT_POLICY).build(
        (replace(_quote(), price=20.0),),
        {"600001": _history_bars()},
        AFTERNOON,
        research_observations={"600001": observation},
    )[0]

    assert observation.financial is not None
    assert observation.financial.report_date == date(2026, 3, 31)
    assert tuple(item.report_date for item in observation.financial_history) == (
        date(2022, 12, 31),
        date(2026, 3, 31),
    )
    assert observation.financial_history_complete is False
    assert len(observation.announcements) == 22
    assert observation.pledge_ratio_pct == pytest.approx(15.0)
    assert observation.pledge_checked_on == AFTERNOON.date()
    assert observation.unlock_ratio_pct == pytest.approx(6.0)
    assert repeated.financial == observation.financial
    assert len(calls) == 9
    assert "api/security/ann" in calls[-1][0]
    assert feature.values["value_score"] == pytest.approx(92.8571428571)
    assert feature.values["growth_score"] == pytest.approx(70.0)
    assert feature.values["quality_score"] == pytest.approx(67.5)
    assert feature.values["pledge_risk"] == 1.0
    assert feature.values["reduction_or_unlock"] == 3.0
    assert feature.values["negative_announcement_level"] == 2.0
    assert {item.evidence_type for item in feature.evidence} >= {
        "financial_snapshot",
        "announcement",
        "ownership_filing",
        "research_summary",
    }
    financial_evidence = next(item for item in feature.evidence if item.evidence_type == "financial_snapshot")
    pledge_evidence = next(item for item in feature.evidence if item.source == "eastmoney_pledge")
    assert "EPS=1" in financial_evidence.title
    assert "core_profit=80" in financial_evidence.title
    assert pledge_evidence.published_at.isoformat() == "2026-07-01T23:59:59+08:00"
    assert all(call[1]["timeout"] == 8 for call in calls)
    assert all(call[1]["proxies"] == {"http": "", "https": "", "all": ""} for call in calls)
    financial_call = next(call for call in calls if "securities/api/data/get" in call[0])
    assert financial_call[1]["params"]["ps"] == "500"
    assert sum("search-api-web" in call[0] for call in calls) == 3


def test_akshare_announcements_query_policy_window_without_claiming_full_history(tmp_path) -> None:
    announcement_pages: list[int] = []

    def announcement_payload(page: int, *, total: int = 250):
        start = (page - 1) * 100
        stop = min(start + 100, total)
        return {
            "data": {
                "list": [
                    {
                        "art_code": f"ann-{index}",
                        "display_time": "2026-07-15 10:00:00:000",
                        "notice_date": "2026-07-15 00:00:00",
                        "title": f"公司日常公告{index}",
                    }
                    for index in range(start, stop)
                ],
                "total_hits": total,
            },
            "success": 1,
        }

    def get(url, **kwargs):
        if "search-api-web" in url:
            callback = "diag"
            return FakeResponse(f'{callback}({{"result": {{"cmsArticleWebOld": []}}}});')
        if "securities/api/data/get" in url:
            return FakeResponse({"result": {"data": []}, "success": True})
        if "api/security/ann" in url:
            page = int(kwargs["params"]["page_index"])
            assert kwargs["params"]["page_size"] == "100"
            assert (
                kwargs["params"]["begin_time"]
                == (AFTERNOON - timedelta(days=LONG_POLICY.announcement_lookback_days)).date().isoformat()
            )
            announcement_pages.append(page)
            return FakeResponse(announcement_payload(page))
        return FakeResponse({"result": {"data": []}, "success": True})

    client = AkshareResearchClient(
        timeout_seconds=8,
        get=get,
        long_research_policy=LONG_POLICY,
        evidence_cache_dir=tmp_path,
    )

    first = client.fetch_snapshot("600001", observed_at=AFTERNOON)
    client.fetch_snapshot("600001", observed_at=AFTERNOON + timedelta(minutes=1))
    refreshed = client.fetch_snapshot("600001", observed_at=AFTERNOON + timedelta(minutes=11))

    assert first.announcements_available is True
    assert refreshed.announcements_available is True
    assert first.corporate_risk_history_complete is False
    assert refreshed.corporate_risk_history_complete is False
    assert announcement_pages == [1, 2, 3, 1, 2, 3]


def test_akshare_announcement_page_failure_does_not_claim_complete_history(tmp_path) -> None:
    def get(url, **kwargs):
        if "search-api-web" in url:
            callback = "diag"
            return FakeResponse(f'{callback}({{"result": {{"cmsArticleWebOld": []}}}});')
        if "securities/api/data/get" in url:
            return FakeResponse({"result": {"data": []}, "success": True})
        if kwargs["params"].get("reportName") == "RPT_CSDC_LIST" and kwargs["params"]["filter"].startswith(
            "(TRADE_DATE<="
        ):
            return FakeResponse({"result": {"data": [{"TRADE_DATE": "2026-07-16"}]}, "success": True})
        if "api/security/ann" in url:
            page = int(kwargs["params"]["page_index"])
            if page == 2:
                raise requests.Timeout("fixture timeout")
            return FakeResponse(
                {
                    "data": {
                        "list": [
                            {
                                "art_code": f"ann-{index}",
                                "display_time": "2026-07-15 10:00:00:000",
                                "title": f"公司日常公告{index}",
                            }
                            for index in range(100)
                        ],
                        "total_hits": 150,
                    },
                    "success": 1,
                }
            )
        return FakeResponse({"result": {"data": []}, "success": True})

    observation = AkshareResearchClient(
        timeout_seconds=8,
        get=get,
        long_research_policy=LONG_POLICY,
        evidence_cache_dir=tmp_path,
    ).fetch_snapshot("600001", observed_at=AFTERNOON)

    assert observation.announcements_available is False
    assert observation.corporate_risk_history_complete is False
    assert observation.source_errors == ("announcements:timeout",)


def test_structured_research_source_failure_preserves_null_and_other_sources() -> None:
    observation = ResearchObservation(
        announcements_available=True,
        pledge_ratio_pct=None,
        unlock_ratio_pct=0.0,
        source_errors=("pledge:timeout",),
    )

    feature = FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY, FEATURE_WEIGHT_POLICY).build(
        (_quote(),),
        {"600001": _history_bars()},
        AFTERNOON,
        research_observations={"600001": observation},
    )[0]

    assert feature.values["pledge_risk"] is None
    assert feature.values["reduction_or_unlock"] == 0.0
    assert "pledge_risk" in feature.missing_fields
    assert feature.values["risk_protection_score"] is not None


def test_partial_research_components_do_not_open_the_shared_source_circuit() -> None:
    observation = ResearchObservation(
        announcements_available=True,
        pledge_ratio_pct=None,
        unlock_ratio_pct=0.0,
        source_errors=("pledge:timeout",),
    )
    client = StaticStructuredResearchClient(
        Evidence("news-1", "news", "普通新闻", "fixture", AFTERNOON),
        observation,
    )
    service = _service(
        StaticGateway((_quote(),)),
        StaticHistoryClient(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY, FEATURE_WEIGHT_POLICY),
        research_client=client,
        research_workers=1,
        research_circuit_breaker_failures=3,
        wall_clock=lambda: AFTERNOON,
    )

    report = service.research.load_report(
        ("600001", "600002", "600003", "600004"),
        AFTERNOON,
        include_structured=True,
        force=True,
    )

    assert tuple(report.observations) == ("600001", "600002", "600003", "600004")
    assert client.snapshot_calls == 4
    assert service.research.status().consecutive_failures == 0
    assert service.research.status().circuit_open is False


def test_successful_component_retry_clears_old_error_and_preserves_verified_history() -> None:
    class RecoveringResearchClient:
        calls = 0

        def fetch_snapshot(self, _code, *, observed_at):
            self.calls += 1
            if self.calls == 1:
                return ResearchObservation(
                    announcements_available=True,
                    corporate_risk_history_complete=True,
                    corporate_risk_evidence_version="verified-history",
                    pledge_ratio_pct=None,
                    unlock_ratio_pct=0.0,
                    source_errors=("pledge:timeout",),
                )
            return ResearchObservation(
                announcements_available=True,
                corporate_risk_history_complete=False,
                pledge_ratio_pct=0.0,
                unlock_ratio_pct=0.0,
            )

    client = RecoveringResearchClient()
    service = _service(
        StaticGateway((_quote(),)),
        StaticHistoryClient(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY, FEATURE_WEIGHT_POLICY),
        research_client=client,
        research_workers=1,
        wall_clock=lambda: AFTERNOON,
    )

    first = service.research.load(("600001",), AFTERNOON, include_structured=True, force=True)["600001"]
    recovered = service.research.load(
        ("600001",),
        AFTERNOON + timedelta(minutes=1),
        include_structured=True,
        force=True,
    )["600001"]

    assert first.source_errors == ("pledge:timeout",)
    assert recovered.source_errors == ()
    assert recovered.pledge_ratio_pct == 0.0
    assert recovered.corporate_risk_history_complete is True
    assert recovered.corporate_risk_evidence_version == "verified-history"


@pytest.mark.parametrize(
    "unlock_row",
    (
        {"FREE_DATE": "invalid", "TOTAL_RATIO": 0.01},
        {"FREE_DATE": "2026-08-01", "TOTAL_RATIO": 1.01},
    ),
)
def test_akshare_structured_research_contains_malformed_source_failure(unlock_row) -> None:
    def get(url, **kwargs):
        if "search-api-web" in url:
            callback = "diag"
            return FakeResponse(f'{callback}({{"result": {{"cmsArticleWebOld": []}}}});')
        if "securities/api/data/get" in url:
            return FakeResponse({"version": "financial-empty", "result": {"data": []}, "success": True})
        if "api/security/ann" in url:
            return FakeResponse({"data": {"list": [], "total_hits": 0}, "success": 1})
        if kwargs["params"].get("reportName") == "RPT_CSDC_LIST" and kwargs["params"]["filter"].startswith(
            "(TRADE_DATE<="
        ):
            return FakeResponse({"success": True, "result": {"data": [{"TRADE_DATE": "2026-07-16"}]}})
        if kwargs["params"].get("reportName") == "RPT_CSDC_LIST":
            return FakeResponse(
                {
                    "version": "pledge-invalid",
                    "result": {"data": [{"TRADE_DATE": "2026-07-16", "PLEDGE_RATIO": "invalid"}]},
                    "success": True,
                }
            )
        return FakeResponse({"version": "unlock-invalid", "result": {"data": [unlock_row]}, "success": True})

    observation = AkshareResearchClient(
        timeout_seconds=8,
        get=get,
        long_research_policy=LONG_POLICY,
    ).fetch_snapshot("600001", observed_at=AFTERNOON)

    assert observation.announcements_available is True
    assert observation.unlock_ratio_pct is None
    assert observation.pledge_ratio_pct is None
    assert observation.pledge_checked_on is None
    assert observation.source_errors == ("pledge:schema_invalid", "unlock:schema_invalid")


def test_akshare_research_rejects_unvalidated_stock_code() -> None:
    client = AkshareResearchClient(get=lambda *_args, **_kwargs: FakeResponse(""))

    with pytest.raises(ValueError, match="six digits"):
        client.fetch_news('600001") OR ("1"="1', observed_at=AFTERNOON)


def test_candidate_news_is_cached_and_failure_does_not_block() -> None:
    news = Evidence("news-1", "news", "公司拟回购股份", "fixture", NOW - timedelta(hours=1))
    research = StaticResearchClient((news,))
    service = _service(
        StaticGateway((_quote(),)),
        StaticHistoryClient(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY, FEATURE_WEIGHT_POLICY),
        research_client=research,
        research_workers=1,
    )

    first = service.fetch_candidate_features(("600001",), NOW)
    second = service.fetch_candidate_features(("600001",), NOW)

    assert research.calls == 1
    assert [item.evidence_id for item in first[0].evidence] == [first[0].evidence[0].evidence_id, "news-1"]
    assert second[0].evidence[-1].evidence_id == "news-1"
    assert first[0].values["news_sentiment"] == 75.0
    assert first[0].values["evidence_freshness"] == 100.0
    assert "news_sentiment" not in first[0].missing_fields

    degraded = _service(
        StaticGateway((_quote(),)),
        StaticHistoryClient(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY, FEATURE_WEIGHT_POLICY),
        research_client=FailingResearchClient(),
        research_workers=1,
    )
    result = degraded.fetch_candidate_features(("600001",), NOW)
    assert len(result) == 1
    assert len(result[0].evidence) == 1
    assert result[0].values["news_sentiment"] is None
    assert result[0].values["evidence_freshness"] is None
    assert "news_sentiment" in result[0].missing_fields
    assert degraded.health()["research_error_count"] == 1
    assert degraded.health()["research_last_error"] == "offline"


def test_structured_research_upgrades_news_only_cache_and_is_reused() -> None:
    news = Evidence("news-1", "news", "公司拟回购股份", "fixture", NOW - timedelta(hours=1))
    research = StaticStructuredResearchClient(
        news,
        ResearchObservation(
            evidence=(news,),
            announcements_available=True,
            pledge_ratio_pct=15.0,
            unlock_ratio_pct=0.0,
        ),
    )
    service = _service(
        StaticGateway((_quote(),)),
        StaticHistoryClient(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY, FEATURE_WEIGHT_POLICY),
        research_client=research,
        research_workers=1,
    )

    news_only = service.fetch_candidate_features(("600001",), NOW)
    first_full = service.fetch_candidate_features(
        ("600001",),
        NOW,
        include_structured_research=True,
    )
    second_full = service.fetch_candidate_features(
        ("600001",),
        NOW,
        include_structured_research=True,
    )

    assert research.news_calls == 1
    assert research.snapshot_calls == 1
    assert news_only[0].values["pledge_risk"] is None
    assert first_full[0].values["pledge_risk"] == 1.0
    assert second_full[0].values["pledge_risk"] == 1.0


def test_read_candidate_features_reuses_structured_research_disk_cache_after_restart(tmp_path) -> None:
    cache_dir = tmp_path / "evidence_cache"
    observation = ResearchObservation(
        financial=FinancialReport(
            report_date=date(2026, 3, 31),
            published_at=datetime.fromisoformat("2026-04-30T23:59:59+08:00"),
            basic_eps=1.0,
            book_value_per_share=10.0,
            revenue_growth_pct=20.0,
            net_profit_growth_pct=10.0,
            core_profit_growth_pct=0.0,
            roe_pct=3.0,
            parent_net_profit=100.0,
            core_net_profit=80.0,
        ),
        announcements_available=True,
    )
    writer = _service(
        StaticGateway((_quote(),)),
        StaticHistoryClient(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY, FEATURE_WEIGHT_POLICY),
        research_client=StaticStructuredResearchClient((), observation),
        research_cache_dir=cache_dir,
        research_workers=1,
        wall_clock=lambda: NOW,
    )

    written = writer.fetch_candidate_features(("600001",), NOW, include_structured_research=True)
    assert written[0].values["quality_score"] == pytest.approx(67.5)

    reader = _service(
        StaticGateway((_quote(),)),
        StaticHistoryClient(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY, FEATURE_WEIGHT_POLICY),
        research_client=None,
        research_cache_dir=cache_dir,
        research_workers=1,
        wall_clock=lambda: NOW,
    )
    reader.fetch_market_features(NOW)

    restored = reader.read_candidate_features(("600001",), NOW, include_structured_research=True)

    assert restored[0].values["value_score"] == pytest.approx(written[0].values["value_score"])
    assert restored[0].values["growth_score"] == pytest.approx(written[0].values["growth_score"])
    assert restored[0].values["quality_score"] == pytest.approx(written[0].values["quality_score"])


def test_research_snapshot_refresh_reuses_fresh_database_observation() -> None:
    observation = ResearchObservation(
        announcements_available=True,
        pledge_ratio_pct=0.0,
        unlock_ratio_pct=0.0,
    )
    research = StaticStructuredResearchClient((), observation)
    service = _service(
        StaticGateway((_quote(),)),
        StaticHistoryClient(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY, FEATURE_WEIGHT_POLICY),
        research_client=research,
        research_workers=1,
    )

    service.refresh_stock_risk(("600001",), AFTERNOON)
    service.refresh_stock_risk(("600001",), AFTERNOON + timedelta(minutes=3))

    assert research.snapshot_calls == 1


def test_research_snapshot_restart_reuses_fresh_persisted_observation(tmp_path: Path) -> None:
    observation = ResearchObservation(
        announcements_available=True,
        pledge_ratio_pct=0.0,
        unlock_ratio_pct=0.0,
    )
    data_plane = SQLiteDataPlane(tmp_path)
    writer_client = StaticStructuredResearchClient((), observation)
    writer = _service(
        StaticGateway((_quote(),)),
        StaticHistoryClient(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY, FEATURE_WEIGHT_POLICY),
        data_plane=data_plane,
        research_client=writer_client,
        research_workers=1,
        wall_clock=lambda: AFTERNOON,
    )
    writer.refresh_stock_risk(("600001",), AFTERNOON)

    reader_client = StaticStructuredResearchClient((), observation)
    restarted_at = AFTERNOON + timedelta(minutes=3)
    reader = _service(
        StaticGateway((_quote(),)),
        StaticHistoryClient(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY, FEATURE_WEIGHT_POLICY),
        data_plane=data_plane,
        research_client=reader_client,
        research_workers=1,
        wall_clock=lambda: restarted_at,
    )
    reader.research.recover_from_data_plane(restarted_at)
    result = reader.refresh_stock_risk(("600001",), restarted_at)

    assert writer_client.snapshot_calls == 1
    assert reader_client.snapshot_calls == 0
    assert result.completed_codes == ("600001",)


def test_stock_risk_refresh_reports_only_real_research_version_changes() -> None:
    monotonic = [0.0]
    wall_clock = [NOW]
    observation = ResearchObservation(
        announcements_available=True,
        corporate_risk_history_complete=True,
        pledge_ratio_pct=0.0,
        unlock_ratio_pct=0.0,
    )
    service = _service(
        StaticGateway((_quote(),)),
        StaticHistoryClient(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY, FEATURE_WEIGHT_POLICY),
        research_client=StaticStructuredResearchClient((), observation),
        research_workers=1,
        research_ttl_seconds=60,
        monotonic=lambda: monotonic[0],
        wall_clock=lambda: wall_clock[0],
    )

    first = service.refresh_stock_risk(("600001",), NOW)
    monotonic[0] = 61.0
    wall_clock[0] = NOW + timedelta(seconds=61)
    unchanged = service.refresh_stock_risk(("600001",), wall_clock[0])

    assert first.changed_codes == ("600001",)
    assert unchanged.completed_codes == ("600001",)
    assert unchanged.changed_codes == ()


def test_stock_risk_batch_deadline_keeps_completed_codes_and_defers_late_codes(market_worker_pool) -> None:
    research = PartiallyBlockingStructuredResearchClient()
    observed_at = datetime.now(timezone.utc)
    service = _service(
        StaticGateway((_quote(),)),
        StaticHistoryClient(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY, FEATURE_WEIGHT_POLICY),
        worker_pool=market_worker_pool,
        research_client=research,
        research_workers=2,
        wall_clock=lambda: datetime.now(timezone.utc),
    )
    release_timer = threading.Timer(0.6, research.release.set)
    release_timer.start()

    try:
        result = service.refresh_stock_risk(
            ("600001", "600002"),
            observed_at,
            deadline=observed_at + timedelta(seconds=0.1),
        )
    finally:
        research.release.set()
        release_timer.cancel()

    assert result.completed_codes == ("600001",)
    assert result.deferred_codes == ("600002",)
    assert result.deadline_reached is True
    assert ("600001", True) in service.research.entries()
    assert ("600002", True) not in service.research.entries()


def test_stock_risk_batch_deadline_discards_only_late_result(market_worker_pool) -> None:
    research = BlockingStructuredResearchClient()
    observed_at = datetime.now(timezone.utc)
    service = _service(
        StaticGateway((_quote(),)),
        StaticHistoryClient(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY, FEATURE_WEIGHT_POLICY),
        worker_pool=market_worker_pool,
        research_client=research,
        research_workers=1,
        wall_clock=lambda: datetime.now(timezone.utc),
    )
    release_timer = threading.Timer(0.6, research.release.set)
    release_timer.start()

    try:
        result = service.refresh_stock_risk(
            ("600001",),
            observed_at,
            deadline=observed_at + timedelta(seconds=0.01),
        )
    finally:
        research.release.set()
        release_timer.cancel()

    assert result.completed_codes == ()
    assert result.deferred_codes == ("600001",)
    assert result.deadline_reached is True
    assert service.health()["research_last_error"] == "research_batch_deadline"
    assert service.research.entries() == {}


def test_research_loader_recover_from_data_plane_overrides_component_statuses(tmp_path: Path) -> None:
    observed_at = datetime(2026, 7, 16, 14, 50, tzinfo=_SHANGHAI)
    source_time = observed_at.replace(minute=49)
    observation = ResearchObservation(
        financial=FinancialReport(
            report_date=date(2026, 2, 28),
            published_at=datetime.fromisoformat("2026-02-28T15:00:00+08:00"),
            basic_eps=1.0,
            book_value_per_share=2.0,
            revenue_growth_pct=5.0,
            net_profit_growth_pct=3.0,
            core_profit_growth_pct=1.0,
            roe_pct=2.0,
            parent_net_profit=10.0,
            core_net_profit=8.0,
        ),
        announcements_available=True,
        corporate_risk_history_complete=True,
        pledge_ratio_pct=0.5,
        unlock_ratio_pct=0.5,
    )
    data_plane = SQLiteDataPlane(tmp_path)

    for component, status in zip(
        RESEARCH_COMPONENT_IDS,
        (
            "known_risk",
            "unknown",
            "unknown",
            "known_clear",
            "stale",
            "known_risk",
            "unknown",
            "known_clear",
        ),
        strict=True,
    ):
        data_plane.save_risk_evidence_recent(
            RiskEvidenceRecord(
                code="600001",
                observed_at=observed_at,
                source_time=source_time,
                source="akshare",
                data_version="akshare-research",
                payload={"status": status},
                evidence_id=f"risk-component:{component}",
                schema_version="data_plane",
            )
        )

    service = _service(
        StaticGateway((_quote(),)),
        StaticHistoryClient(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY, FEATURE_WEIGHT_POLICY),
        data_plane=data_plane,
        research_client=StaticStructuredResearchClient((), observation),
        research_workers=1,
        wall_clock=lambda: observed_at,
    )
    service.research.recover_from_data_plane()
    service.refresh_stock_risk(("600001",), observed_at)
    status = service.research.status()

    assert status.unavailable_count == 0
    assert status.partial_count == 1
    assert status.verified_count == 0
    assert status.financial_covered_count == 1
    assert status.announcements_covered_count == 0
    assert status.pledge_covered_count == 0
    assert status.unlock_covered_count == 1


def test_research_loader_uses_bounded_risk_batches() -> None:
    class BatchClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, ...]] = []

        def fetch_risk_snapshots(self, codes, *, observed_at):
            self.calls.append(tuple(codes))
            assert observed_at == NOW
            return {code: ResearchObservation(pledge_ratio_pct=0.0) for code in codes}

    class BatchDataPlane:
        def __init__(self) -> None:
            self.batch_sizes: list[int] = []

        def load_risk_evidence_recent_records(self, codes=None):
            return ()

        def load_source_cursor_recent_records(self, cursor_names=None):
            return ()

        def save_risk_evidence_recent_records(self, records) -> None:
            self.batch_sizes.append(len(records))

    batch_client = BatchClient()
    data_plane = BatchDataPlane()
    structured_client = StaticStructuredResearchClient((), ResearchObservation())
    workers = BoundedExecutor(worker_count=2, queue_capacity=4, thread_name_prefix="risk-batch-test")
    workers.start()
    try:
        service = _service(
            StaticGateway((_quote(),)),
            StaticHistoryClient(),
            FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY, FEATURE_WEIGHT_POLICY),
            data_plane=data_plane,
            research_client=structured_client,
            risk_batch_client=batch_client,
            worker_pool=workers,
            research_cache_limit=120,
            wall_clock=lambda: NOW,
        )
        codes = tuple(f"{index:06d}" for index in range(101))

        report = service.research.load_report(
            codes,
            NOW,
            include_structured=True,
            deadline=NOW + timedelta(seconds=5),
        )
        service.research.load_report(
            (codes[0],),
            NOW,
            include_structured=True,
            force=True,
            deadline=NOW + timedelta(seconds=5),
        )
    finally:
        workers.stop()

    assert sorted(len(call) for call in batch_client.calls) == [1, 50, 50]
    assert set(report.observations) == set(codes)
    assert len(report.changed_codes) == 101
    assert report.deferred_codes == ()
    assert report.deadline_reached is False
    assert structured_client.snapshot_calls == 1
    assert sorted(data_plane.batch_sizes) == [9, 9, 450, 450]


def test_research_component_same_time_conflict_preserves_first_committed_status(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    class ComponentState:
        def __init__(self) -> None:
            self._lock = threading.Lock()
            self._component_statuses = {}

    observed_at = datetime(2026, 8, 13, 10, 16, 30, tzinfo=_SHANGHAI)
    state = ComponentState()
    data_plane = SQLiteDataPlane(tmp_path)

    PersistResearchComponentStatuses(
        state,
        data_plane,
        "603083",
        observed_at,
        ResearchObservation(),
    )
    PersistResearchComponentStatuses(
        state,
        data_plane,
        "603083",
        observed_at,
        ResearchObservation(pledge_ratio_pct=0.0),
    )

    records = data_plane.load_risk_evidence_recent_records(codes=("603083",))
    records_by_id = {record.evidence_id: record for record in records}
    assert len(records_by_id) == len(RESEARCH_COMPONENT_IDS) + 1
    assert "risk-observation:structured" in records_by_id
    assert records_by_id["risk-component:pledge"].payload == {"status": "unknown"}
    assert records_by_id["risk-component:penalty"].payload == {"status": "unknown"}
    assert "research risk component persistence failed" not in caplog.text


def test_news_research_does_not_persist_risk_components() -> None:
    class CountingRiskDataPlane:
        def __init__(self) -> None:
            self.save_calls = 0

        def load_risk_evidence_recent_records(self, codes: list[str] | None = None) -> tuple[RiskEvidenceRecord, ...]:
            return ()

        def save_risk_evidence_recent(self, _record: RiskEvidenceRecord) -> None:
            self.save_calls += 1

        def save_risk_evidence_recent_records(self, records) -> None:
            self.save_calls += len(records)

    data_plane = CountingRiskDataPlane()
    service = _service(
        StaticGateway((_quote(),)),
        StaticHistoryClient(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY, FEATURE_WEIGHT_POLICY),
        data_plane=data_plane,
        research_client=StaticResearchClient(()),
        research_workers=1,
        wall_clock=lambda: NOW,
    )

    service.fetch_candidate_features(("600001",), NOW, include_structured_research=False)

    assert data_plane.save_calls == 0


def test_research_data_plane_persistence_unavailable_does_not_block_research_load() -> None:
    class FailingRiskSaveDataPlane:
        def __init__(self) -> None:
            self.recovered = False

        def load_risk_evidence_recent_records(self, codes: list[str] | None = None) -> tuple[RiskEvidenceRecord, ...]:
            return ()

        def save_risk_evidence_recent(self, _record: RiskEvidenceRecord) -> None:
            raise DataPlaneUnavailableError("unavailable")

        def save_risk_evidence_recent_records(self, _records) -> None:
            raise DataPlaneUnavailableError("unavailable")

    service = _service(
        StaticGateway((_quote(),)),
        StaticHistoryClient(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY, FEATURE_WEIGHT_POLICY),
        data_plane=FailingRiskSaveDataPlane(),
        research_client=StaticStructuredResearchClient(
            (),
            ResearchObservation(
                announcements_available=True,
                pledge_ratio_pct=0.0,
                unlock_ratio_pct=0.0,
            ),
        ),
        research_workers=1,
        wall_clock=lambda: NOW,
    )

    result = service.refresh_stock_risk(("600001",), NOW)

    assert result.completed_codes == ("600001",)
    assert ("600001", True) in service.research.entries()
