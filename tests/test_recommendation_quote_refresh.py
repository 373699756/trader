import threading
import time
from unittest.mock import Mock, patch

import pandas as pd

from stock_analyzer.candidate_pipeline import CandidatePipeline
from stock_analyzer.services.recommendation_quotes import RecommendationQuoteRefreshService


class _Config:
    RECOMMENDATION_CANDIDATE_POOL_SIZE = 1


def _quotes(code="600001", price=10.0):
    frame = pd.DataFrame(
        [
            {
                "code": code,
                "price": price,
                "pct_chg": 1.0,
                "turnover": 100.0,
                "volume_ratio": 1.2,
            }
        ]
    )
    frame.attrs["quote_timestamp"] = "2026-07-15T10:00:00"
    return frame


def _wait_until(predicate, timeout_seconds=1.0):
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def _service(fetch_quotes, *, full_quotes=None, thread_factory=None):
    display_cache = Mock()
    clear_recommendation = Mock()
    clear_horizon = Mock()
    service = RecommendationQuoteRefreshService(
        fetch_quotes=fetch_quotes,
        load_full_quotes=Mock(return_value=full_quotes),
        cache_display_quotes=display_cache,
        clear_recommendation_cache=clear_recommendation,
        clear_horizon_cache=clear_horizon,
        config_source=_Config(),
        thread_factory=thread_factory,
    )
    return service, display_cache, clear_recommendation, clear_horizon


def test_display_refresh_owns_snapshot_and_publishes_cache():
    source = _quotes()
    fetch_quotes = Mock(side_effect=lambda _codes: source.copy(deep=True))
    service, display_cache, _, _ = _service(fetch_quotes)
    assert not hasattr(service, "owner")
    assert not hasattr(service, "container")
    assert not hasattr(service, "pipeline")

    initial, initial_status = service.recommendation_quotes(["sh600001"])
    assert initial.empty
    assert initial_status in {"", "推荐池行情后台刷新中"}
    assert _wait_until(lambda: service.status()["display"]["success_count"] == 1)

    snapshot, _ = service.recommendation_quotes(["600001"])
    snapshot.loc[0, "price"] = 999.0
    service.stop(1.0)

    assert snapshot.iloc[0]["code"] == "600001"
    assert display_cache.call_count >= 1
    cached = display_cache.call_args_list[0].args[0]
    assert cached.iloc[0]["price"] == 10.0
    assert service.status()["display"]["running"] is False


def test_thread_start_failure_recovers_without_stuck_running_state():
    factory_calls = 0

    class FailingThread:
        def start(self):
            raise RuntimeError("thread unavailable")

        def is_alive(self):
            return False

    def thread_factory(**kwargs):
        nonlocal factory_calls
        factory_calls += 1
        if factory_calls == 1:
            return FailingThread()
        return threading.Thread(**kwargs)

    service, _, _, _ = _service(Mock(return_value=_quotes()), thread_factory=thread_factory)

    with patch("stock_analyzer.services.recommendation_quotes._LOGGER.exception"):
        service.recommendation_quotes(["600001"])
    failed = service.status()["display"]
    assert failed["running"] is False
    assert failed["failure_count"] == 1
    assert failed["error"] == "thread unavailable"

    service.recommendation_quotes(["600001"])
    assert _wait_until(lambda: service.status()["display"]["success_count"] == 1)
    service.stop(1.0)


def test_stop_waits_for_active_fetch_rejects_new_work_and_allows_restart():
    fetch_started = threading.Event()
    release_fetch = threading.Event()
    stopped = threading.Event()
    fetch_count = 0

    def fetch_quotes(_codes):
        nonlocal fetch_count
        fetch_count += 1
        if fetch_count == 1:
            fetch_started.set()
            assert release_fetch.wait(1.0)
        return _quotes()

    service, _, _, _ = _service(fetch_quotes)
    service.recommendation_quotes(["600001"])
    assert fetch_started.wait(1.0)

    def stop_service():
        service.stop(1.0)
        stopped.set()

    stop_thread = threading.Thread(target=stop_service)
    stop_thread.start()
    assert _wait_until(lambda: service.status()["display"]["stopping"] is True)
    service.recommendation_quotes(["600002"])
    assert fetch_count == 1

    release_fetch.set()
    assert stopped.wait(1.0)
    stop_thread.join(1.0)
    assert service.status()["display"]["stopping"] is False

    service.recommendation_quotes(["600001"])
    assert _wait_until(lambda: fetch_count == 2)
    service.stop(1.0)


def test_candidate_refresh_selects_pool_overlays_quotes_and_invalidates_payload_caches():
    full_quotes = pd.DataFrame(
        [
            {"code": "600001", "price": 10.0, "pct_chg": 1.0, "turnover": 10.0, "volume_ratio": 1.0},
            {"code": "600002", "price": 20.0, "pct_chg": 5.0, "turnover": 100.0, "volume_ratio": 2.0},
        ]
    )

    def fetch_quotes(codes):
        assert codes == ["600002"]
        return _quotes(code="600002", price=22.0)

    service, _, clear_recommendation, clear_horizon = _service(fetch_quotes, full_quotes=full_quotes)
    service.refresh_groups({"candidate_seconds": 0})
    assert _wait_until(lambda: service.status()["candidate"]["success_count"] == 1)

    overlaid = service.overlay_candidate_quotes(full_quotes)
    service.stop(1.0)

    assert overlaid.loc[overlaid["code"] == "600002", "price"].iloc[0] == 22.0
    assert overlaid.attrs["candidate_quote_timestamp"] == "2026-07-15T10:00:00"
    clear_recommendation.assert_called_once_with()
    clear_horizon.assert_called_once_with()


def test_display_and_candidate_fetches_share_one_network_slot():
    display_started = threading.Event()
    release_display = threading.Event()
    candidate_started = threading.Event()
    calls = []
    full_quotes = pd.DataFrame(
        [{"code": "600002", "price": 20.0, "pct_chg": 5.0, "turnover": 100.0, "volume_ratio": 2.0}]
    )

    def fetch_quotes(codes):
        calls.append(list(codes))
        if codes == ["600001"]:
            display_started.set()
            assert release_display.wait(1.0)
        else:
            candidate_started.set()
        return _quotes(code=codes[0])

    service, _, _, _ = _service(fetch_quotes, full_quotes=full_quotes)
    service.recommendation_quotes(["600001"])
    assert display_started.wait(1.0)
    service.refresh_groups({"candidate_seconds": 0})
    assert not candidate_started.wait(0.05)

    release_display.set()
    assert candidate_started.wait(1.0)
    service.stop(1.0)

    assert calls == [["600001"], ["600002"]]


def test_candidate_pipeline_keeps_legacy_capability_composition_path():
    class Cache:
        def __init__(self, value=None):
            self.value = value

        def get(self):
            return self.value

        def set(self, value):
            self.value = value

        def clear(self):
            self.value = None

    class Caches:
        quotes_cache = Cache(_quotes())
        recommendation_quotes_cache = Cache()
        recommendation_cache = Cache()
        horizon_cache = Cache()

    class Provider:
        def get_recommendation_quotes(self, _codes):
            return _quotes()

    pipeline = CandidatePipeline(Provider(), Caches())

    assert isinstance(pipeline.quote_refresh_service, RecommendationQuoteRefreshService)
    pipeline.recommendation_quotes(["600001"])
    assert _wait_until(lambda: pipeline.recommendation_quote_status()["display"]["success_count"] == 1)
    pipeline.quote_refresh_service.stop(1.0)
