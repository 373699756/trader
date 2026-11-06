import pytest

from trader.infra.workers import BoundedExecutor
from tests.helpers.shared.worker_cleanup import StopFixtureWorkers


@pytest.fixture
def training_worker_pool():
    pool = BoundedExecutor(worker_count=5, queue_capacity=5, thread_name_prefix="test-training-worker")
    pool.start()
    try:
        yield pool
    finally:
        StopFixtureWorkers(pool)
