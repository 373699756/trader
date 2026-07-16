import threading
from unittest.mock import Mock

from stock_analyzer.providers import TimedCache
from stock_analyzer.services.factor_sentiment_refresh import FactorSentimentRefreshService


def _service(
    *,
    refresh_history=None,
    score_sentiment=None,
    cache=None,
    **kwargs,
):
    return FactorSentimentRefreshService(
        refresh_history=refresh_history or Mock(return_value={"failed": 0}),
        score_sentiment=score_sentiment or Mock(return_value={"score": 68.0}),
        sentiment_cache=cache if cache is not None else TimedCache(60),
        normalize_code=lambda value: str(value or "")[-6:],
        **kwargs,
    )


def test_sentiment_returns_stale_cache_while_single_flight_refresh_runs():
    cache = TimedCache(60)
    cache.set(
        {
            "entries": {
                "600001": {
                    "value": {"score": 71.0, "summary": "旧缓存", "risk_words": []},
                    "expires_at": 1.0,
                }
            },
            "refreshing": set(),
        }
    )
    started = threading.Event()
    release = threading.Event()

    def score_sentiment(_code, _name):
        started.set()
        if not release.wait(timeout=2.0):
            raise TimeoutError("test did not release sentiment scorer")
        return {"score": 82.0, "summary": "新缓存", "risk_words": []}

    scorer = Mock(side_effect=score_sentiment)
    service = _service(score_sentiment=scorer, cache=cache)
    candidates = [{"code": "600001", "name": "样本"}]

    try:
        first = service.sentiment_for_candidates(candidates)
        assert started.wait(timeout=2.0)
        second = service.sentiment_for_candidates(candidates)

        assert first["600001"]["summary"] == "旧缓存"
        assert second["600001"]["summary"] == "旧缓存"
        assert scorer.call_count == 1
        assert service.status()["sentiment"]["refreshing_items"] == 1
    finally:
        release.set()
        service.stop(timeout_seconds=2.0)

    cached = cache.get()
    assert cached["entries"]["600001"]["value"]["summary"] == "新缓存"
    assert cached["refreshing"] == set()
    assert service.status()["sentiment"]["success_count"] == 1


def test_stop_rejects_new_history_work_until_running_batch_finishes():
    started = threading.Event()
    release = threading.Event()
    calls = []

    def refresh_history(codes):
        calls.append(tuple(codes))
        started.set()
        if not release.wait(timeout=2.0):
            raise TimeoutError("test did not release history refresher")
        return {"failed": 0}

    service = _service(refresh_history=refresh_history)
    assert service.schedule_history(["600001", "600001"]) is True
    assert started.wait(timeout=2.0)
    assert service.schedule_history(["600001"]) is False

    service.stop(timeout_seconds=0.0)

    assert service.status()["stopping"] is True
    assert service.schedule_history(["600002"]) is False

    release.set()
    service.stop(timeout_seconds=2.0)

    assert calls == [("600001",)]
    assert service.status()["stopping"] is False
    assert service.status()["history"]["refreshing_items"] == 0
    assert service.status()["history"]["success_count"] == 1


def test_thread_start_failure_releases_sentiment_single_flight_state():
    cache = TimedCache(60)

    def fail_thread_factory(**_kwargs):
        raise RuntimeError("thread unavailable")

    service = _service(cache=cache, thread_factory=fail_thread_factory)

    lookup = service.sentiment_for_candidates([{"code": "600001", "name": "样本"}])

    assert lookup["600001"]["summary"] == "舆情刷新中"
    status = service.status()["sentiment"]
    assert status["active_threads"] == 0
    assert status["refreshing_items"] == 0
    assert status["failure_count"] == 1
    assert status["last_error"] == "thread unavailable"
    assert cache.get()["refreshing"] == set()


def test_partial_history_failure_is_observable_and_reported():
    errors = []
    service = _service(
        refresh_history=lambda _codes: {"failed": 1, "errors": ["database unavailable"]},
        record_error=errors.append,
    )

    assert service.schedule_history(["600001"]) is True
    service.stop(timeout_seconds=2.0)

    status = service.status()["history"]
    assert status["failure_count"] == 1
    assert status["refreshing_items"] == 0
    assert "historical factor refresh" in status["last_error"]
    assert any("后台历史因子刷新失败" in message for message in errors)
