import pytest

from trader.infra.workers import BoundedExecutor
from tests.helpers.shared.worker_cleanup import StopFixtureWorkers


@pytest.fixture
def market_worker_pool():
    pool = BoundedExecutor(worker_count=2, queue_capacity=4, thread_name_prefix="test-market-worker")
    pool.start()
    try:
        yield pool
    finally:
        StopFixtureWorkers(pool)
