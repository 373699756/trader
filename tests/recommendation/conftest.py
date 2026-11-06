import pytest

from trader.infra.workers import BoundedExecutor


@pytest.fixture
def market_worker_pool():
    pool = BoundedExecutor(worker_count=2, queue_capacity=4, thread_name_prefix="test-market-worker")
    pool.start()
    try:
        yield pool
    finally:
        pool.stop(cancel_futures=True)
